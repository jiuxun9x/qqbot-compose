"""Presentation policy, independent of provider selection and stored history."""
import base64
from io import BytesIO
import re

from markdown_it import MarkdownIt
from PIL import Image


MAX_RENDER_PAGES = 24


PRESENTATION_PROMPT = r'''回复使用内部排版标记：第一行只写以下三者之一，随后换行给正文。
首先区分“请求答题”和“日常聊天”：明确的作业、考试、简答题、阅读题等，即使答案只有一句，也必须选 latex 或 markdown 转成图片；不能因为是历史、语文、地理等文科题就选 plain。谈学习感受等闲聊不属于请求答题。
<qqbot:plain>：仅限闲聊、非答题的日常事实小问答、情绪交流，默认一到两句、20～60 字，最多约 80 字；正文只用自然段纯文本，不用 Markdown 标题、星号、反引号。对方明确要求详细时才展开。
<qqbot:latex>：数学、物理推导、证明、计算或主要含公式的解题，正文用 Markdown 加 LaTeX 数学公式；系统会按用户的 ElegantBook 模板转成图片。
数学概念解释、公式含义、条件从何而来、为什么成立等理论问答，只要涉及公式、数学论证或推导，也必须选 latex；不是只有求数值或明确写“做题”才排版。
<qqbot:markdown>：代码、算法实现、排错、操作步骤，以及历史、语文、阅读理解等其他非数学题目，正文用标准 Markdown，代码放在带语言的三反引号代码块里；系统会按清楚的 Markdown 模板转成图片。
按本次回答的主体选择一个，复杂数学题里的少量代码仍选 latex；代码题中的少量公式选 markdown。
只有 plain 是普通聊天文字；两个题目模式都只输出正文，不包裹整篇代码围栏，不说“下面是图片”，不输出完整 LaTeX 文档、导言区或宏定义。
数学行内公式用 $...$，独立公式用 $$...$$，只用标准数学命令，长推导用 aligned 等环境合理分行。
尽量不手写公式编号；若需要 \tag{...}，必须放在整个 aligned、gathered 等环境之外，不能放在其内部或行内公式中。
数学模式可以用 ::: {.theorem title="原有定理标题"} / ::: 标记定理块；可用 definition、lemma、corollary、proposition、proof、problem、analysis、supplement。
计算和求解过程优先放在 analysis 块中；若写题面，problem 块只能准确引用用户原题，不添加条件。
证明用独立的 proof 块并放在定理块外，不重复手写证明标签；题目和已有解析才用 problem/analysis 块。不要为了用框而添加无关定义或补充结论。
排版标记是系统控制字段，只在第一行写一次，不在结尾添加关闭标签，不解释给用户。题目解答仍需完整、准确；不得用排版代替解答。
'''

_PARSER = MarkdownIt('commonmark', {'html': False}).enable('table')
MARKER = re.compile(r'^\s*<qqbot:(plain|latex|markdown)>\s*\n?', re.I)
_CLOSING_MARKERS = re.compile(r'(?:\s*</qqbot:(?:plain|latex|markdown)>)+\s*\Z', re.I)
_MATH = re.compile(r'\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|(?<!\$)\$[^\n$]+\$', re.S)
_MATH_BLOCK = re.compile(r'^\s*:{3,}\s*\{\.(?:definition|theorem|lemma|corollary|proposition|proof|problem|analysis|supplement)\b', re.M)


def _visible_prose(text):
    """Ignore literal source examples when detecting answer formatting."""
    return '\n'.join(token.content for token in _PARSER.parse(text) if token.type == 'inline')


def has_math_markup(text):
    prose = _visible_prose(text)
    if _MATH_BLOCK.search(prose):
        return True
    for match in _MATH.finditer(prose):
        value = match.group(0)
        if value.startswith('$$') or value.startswith('\\'):
            return True
        # Two currency prices such as "$5，剩 $3" are not a TeX formula.
        if re.search(r'\\[A-Za-z]+|[=+*/^_<>]|[A-Za-z]', value[1:-1]):
            return True
        if re.fullmatch(r'\$[\d\s.,−-]+\$', value) and not prose[match.end():match.end()+1].isdigit():
            return True
    return False


def choose_presentation(text):
    """Honor the model's mode, but never deliver math/template markup as chat."""
    text = str(text or '')
    marker = MARKER.match(text)
    body = _CLOSING_MARKERS.sub('', text[marker.end():] if marker else text).strip()
    if marker:
        style = marker.group(1).lower()
        if style == 'plain' and has_math_markup(body):
            style = 'latex'
        return style, body
    if re.search(r'^\s*```', body, re.M):
        return 'markdown', body
    if has_math_markup(body):
        return 'latex', body
    return 'plain', body


def plain_text(text):
    """Parse formatting, rather than deleting literal * / # / URL characters."""
    _, text = choose_presentation(text)
    parts = []
    for token in _PARSER.parse(text):
        if token.type == 'inline':
            links = []
            for child in token.children or []:
                if child.type in ('text', 'code_inline', 'image'):
                    parts.append(child.content)
                elif child.type in ('softbreak', 'hardbreak'):
                    parts.append('\n')
                elif child.type == 'link_open':
                    links.append((len(parts), child.attrGet('href') or ''))
                elif child.type == 'link_close' and links:
                    start, url = links.pop()
                    if url and ''.join(parts[start:]) != url:
                        parts.append('（' + url + '）')
        elif token.type in ('fence', 'code_block'):
            parts.append(token.content.rstrip() + '\n\n')
        elif token.type in ('paragraph_close', 'heading_close', 'list_item_close', 'tr_close'):
            parts.append('\n\n')
        elif token.type == 'th_close' or token.type == 'td_close':
            parts.append('  ')
    return re.sub(r'\n{3,}', '\n\n', ''.join(parts)).strip()


def validate_image(encoded, max_bytes=12 * 1024**2):
    if not isinstance(encoded, str) or len(encoded) > max_bytes * 4 // 3 + 8:
        raise ValueError('image_size_limit')
    data = base64.b64decode(encoded, validate=True)
    if len(data) > max_bytes:
        raise ValueError('image_size_limit')
    with Image.open(BytesIO(data)) as image:
        if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 20_000_000:
            raise ValueError('unsupported_image')
        image.verify()
    return encoded
