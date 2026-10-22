"""Private, serial, deterministic Markdown/math -> PDF -> PNG renderer.

No credentials, host data, external URLs, shell execution, or browser engine.
The document class is ElegantBook v4.7.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from PIL import Image, ImageChops

MAX_INPUT = 32000
MAX_PAGES = 24
MAX_BODY = 160000
ASSETS = Path(__file__).resolve().parent / 'assets'
ROOT = ASSETS.parent
BUSY = threading.BoundedSemaphore(1)


class RenderError(Exception):
    pass


def _child_limits():
    resource.setrlimit(resource.RLIMIT_CPU, (22, 24))
    resource.setrlimit(resource.RLIMIT_FSIZE, (24 * 1024**2, 24 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run(args, folder, deadline, data=None):
    remaining = min(25, deadline - time.monotonic())
    if remaining <= 0:
        raise RenderError('render_timeout')
    env = {**os.environ, 'openin_any': 'p', 'openout_any': 'p', 'shell_escape': 'f',
           'TEXMFOUTPUT': str(folder), 'TEXMFVAR': str(folder / 'texmf-var'),
           'TEXMFCONFIG': str(folder / 'texmf-config')}
    process = subprocess.Popen(args, cwd=folder, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                               start_new_session=True, preexec_fn=_child_limits)
    try:
        out, err = process.communicate(data, timeout=remaining)
    except (subprocess.TimeoutExpired, BaseException):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise RenderError('render_timeout') from None
    if process.returncode:
        # Logs remain inside the ephemeral job, never server/QQ logs.
        (folder / 'diagnostic.txt').write_bytes(out[-12000:] + b'\n' + err[-4000:])
        raise RenderError('compile_failed_' + Path(args[0]).name)
    return out


FORBIDDEN_MATH = re.compile(
    r'\\(?:input|include|includeonly|usepackage|documentclass|write\d*|openin|openout|'
    r'read|readline|closein|closeout|immediate|catcode|csname|endcsname|expandafter|'
    r'noexpand|def|gdef|edef|xdef|let|futurelet|loop|repeat|every\w*|special|'
    r'shipout|directlua|lua\w*|pdf\w*|XeTeX\w*|font|newcommand|renewcommand|'
    r'newenvironment|renewenvironment|scantokens|toks\d*|errmessage|message|'
    r'endinput|makeatletter|makeatother|global|advance|multiply|divide|count\d*)\b', re.I)
MATH_ENVS = {'aligned', 'alignedat', 'gathered', 'split', 'cases', 'matrix', 'pmatrix',
             'bmatrix', 'Bmatrix', 'vmatrix', 'Vmatrix', 'smallmatrix', 'array'}


def normalize_math_tags(text, display):
    r"""Move a single equation number outside a nested alignment environment.

    amsmath forbids \tag inside aligned/gathered/etc.  Keep its exact label and
    expression, and attach it to the enclosing display instead.  Multiple tags
    and inline math are left alone rather than guessing equation structure.
    """
    tags = list(re.finditer(r'\\tag\*?\s*\{', text))
    if not display or len(tags) != 1:
        return text
    match = tags[0]
    stack = []
    for env in re.finditer(r'\\(begin|end)\s*\{([^{}]+)\}', text[:match.start()]):
        if env.group(1) == 'begin':
            stack.append(env.group(2))
        elif stack and stack[-1] == env.group(2):
            stack.pop()
        else:
            return text
    if not stack or any(env not in MATH_ENVS for env in stack):
        return text
    end, depth = match.end(), 1
    while end < len(text) and depth:
        if text[end] == '\\':
            end += 2  # Escaped braces do not close the tag argument.
            continue
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    if depth:
        return text
    return text[:match.start()] + text[end:] + '\n' + text[match.start():end]


def sanitize_ast(node):
    if isinstance(node, list):
        return [sanitize_ast(x) for x in node]
    if not isinstance(node, dict):
        return node
    tag, value = node.get('t'), node.get('c')
    if tag in ('RawBlock', 'RawInline'):
        raise RenderError('raw_markup_disabled')
    if tag == 'Math':
        text = value[1]
        if '^^' in text or FORBIDDEN_MATH.search(text):
            raise RenderError('unsupported_math_command')
        for env in re.findall(r'\\(?:begin|end)\s*\{([^{}]+)\}', text):
            if env not in MATH_ENVS:
                raise RenderError('unsupported_math_environment')
        return {**node, 'c': [value[0], normalize_math_tags(text, value[0]['t'] == 'DisplayMath')]}
    if tag == 'Image':
        # A text answer can label an image, but cannot fetch arbitrary URLs.
        return {'t': 'Emph', 'c': sanitize_ast(value[1]) or [{'t': 'Str', 'c': '图片'}]}
    if tag == 'Link':
        if not re.match(r'^https?://', value[2][0], re.I):
            return {'t': 'Span', 'c': [['', [], []], sanitize_ast(value[1])]}
    if tag == 'CodeBlock':
        # Unique literal environment prevents code containing \end{verbatim}
        # from escaping its own display block. Its contents are never executed.
        code = value[1]
        name = 'QQCode' + secrets.token_hex(10)
        text = ('\\DefineVerbatimEnvironment{' + name + '}{Verbatim}{breaklines=true,'
                'breakanywhere=true,fontsize=\\small,frame=single,framesep=6pt}\n'
                '\\begin{' + name + '}\n' + code + '\n\\end{' + name + '}')
        return {'t': 'RawBlock', 'c': ['latex', text]}
    return {key: sanitize_ast(val) for key, val in node.items()}


def compact_png(path):
    """Crop only unused page margins/gap above footer in the QQ PNG.

    The PDF and every content pixel keep the original template; short answers
    should not become screenshots containing an entire empty A4 page.
    """
    with Image.open(path) as source:
        source = source.convert('RGB')
        width, height = source.size
        ink = ImageChops.difference(source, Image.new('RGB', source.size, 'white')).convert('L').point(lambda p: 255 if p > 8 else 0)
        bounds = ink.getbbox()
        if not bounds:
            return
        left, top, right, bottom = bounds
        cut = int(height * 0.85)
        footer = ink.crop((0, cut, width, height)).getbbox()
        body = ink.crop((0, 0, width, cut)).getbbox()
        x0, x1 = max(0, left - 24), min(width, right + 24)
        y0 = max(0, top - 24)
        if (body and footer and footer[2] - footer[0] < width * 0.25
                and footer[3] - footer[1] < 60 and cut + footer[1] - body[3] > 160):
            body_end = min(height, body[3] + 16)
            footer_start = max(0, cut + footer[1] - 8)
            footer_end = min(height, cut + footer[3] + 16)
            result = Image.new('RGB', (x1 - x0, body_end - y0 + 20 + footer_end - footer_start), 'white')
            result.paste(source.crop((x0, y0, x1, body_end)), (0, 0))
            result.paste(source.crop((x0, footer_start, x1, footer_end)), (0, body_end - y0 + 20))
        else:
            result = source.crop((x0, y0, x1, min(height, bottom + 24)))
        result.save(path)


def render_document(content, style, folder):
    if style not in ('latex', 'markdown') or not isinstance(content, str) or not content.strip():
        raise RenderError('invalid_document')
    if len(content) > MAX_INPUT:
        raise RenderError('document_too_long')
    deadline = time.monotonic() + 55
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    source = 'markdown+fenced_divs+lists_without_preceding_blankline+tex_math_dollars+tex_math_single_backslash-raw_tex-raw_html'
    ast = json.loads(run(['pandoc', '-f', source, '-t', 'json'], folder, deadline, content.encode()))
    ast = sanitize_ast(ast)
    args = ['pandoc', '-f', 'json', '-t', 'latex', '--no-highlight', '--wrap=none']
    if style == 'latex':
        args += ['--lua-filter', str(ROOT / 'blocks.lua')]
    body = run(args, folder, deadline, json.dumps(ast, ensure_ascii=False).encode()).decode()
    if style == 'latex':
        shutil.copyfile(ASSETS / 'elegantbook.cls', folder / 'elegantbook.cls')
        preamble = (ASSETS / 'preamble.tex').read_text()
        preamble += '\n\\usepackage{longtable,fvextra}\n\\setcounter{secnumdepth}{0}\n'
        preamble += '\\renewcommand{\\sectionmark}[1]{\\markboth{#1}{#1}}\n'
    else:
        preamble = (ROOT / 'md-preamble.tex').read_text()
    preamble += r'''
\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}
\providecommand{\passthrough}[1]{#1}
\providecommand{\pandocbounded}[1]{#1}
'''
    (folder / 'main.tex').write_text(preamble + '\n\\begin{document}\n' + body + '\n\\end{document}\n')
    for _ in range(2):
        run(['xelatex', '-no-shell-escape', '-halt-on-error', '-interaction=nonstopmode', 'main.tex'], folder, deadline)
    pdf = folder / 'main.pdf'
    info = run(['pdfinfo', str(pdf)], folder, deadline).decode()
    match = re.search(r'^Pages:\s*(\d+)', info, re.M)
    count = int(match.group(1)) if match else 0
    if not 0 < count <= MAX_PAGES:
        raise RenderError('page_limit_exceeded')
    run(['pdftoppm', '-r', '150', '-png', str(pdf), str(folder / 'page')], folder, deadline)
    images = sorted(folder.glob('page-*.png'))
    for path in images:
        compact_png(path)
    if len(images) != count or sum(p.stat().st_size for p in images) > 12 * 1024**2:
        raise RenderError('image_limit_exceeded')
    log = (folder / 'main.log').read_text(errors='replace')
    return {'pages': [base64.b64encode(p.read_bytes()).decode() for p in images],
            'page_count': count, 'format': style,
            'elapsed_ms': round((time.monotonic() - (deadline - 55)) * 1000),
            'warnings': {'overfull': len(re.findall('Overfull', log)),
                         'missing_glyphs': len(re.findall('Missing character:', log))}}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *args):
        pass

    def respond(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.respond(200 if self.path == '/health' else 404, {'status': 'ok'} if self.path == '/health' else {'error': 'not_found'})

    def do_POST(self):
        if self.path != '/render':
            self.respond(404, {'error': 'not_found'})
            return
        if not BUSY.acquire(blocking=False):
            self.respond(409, {'error': 'renderer_busy'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY:
                raise RenderError('body_limit_exceeded')
            request = json.loads(self.rfile.read(size))
            with tempfile.TemporaryDirectory(prefix='qqbot-render-') as temp:
                result = render_document(request.get('content'), request.get('format'), Path(temp))
                if request.get('include_pdf') is True:
                    result['pdf'] = base64.b64encode((Path(temp) / 'main.pdf').read_bytes()).decode()
                self.respond(200, result)
        except (ValueError, TypeError, RenderError) as exc:
            code = str(exc) if isinstance(exc, RenderError) else 'invalid_request'
            print(json.dumps({'render_error': code}), flush=True)
            self.respond(422, {'error': code})
        except Exception as exc:
            print(json.dumps({'render_error': type(exc).__name__}), flush=True)
            self.respond(500, {'error': 'renderer_failed'})
        finally:
            BUSY.release()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
