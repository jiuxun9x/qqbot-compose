-- Only our own fixed wrappers become raw TeX. Raw TeX/HTML in input is disabled.
local envs = {
  definition='definition*', theorem='theorem*', lemma='lemma*',
  corollary='corollary*', proposition='proposition*', proof='proof',
  problem='checkedproblem', analysis='analysisbox', supplement='supplementbox'
}
local function escape(s)
  local map = {['\\']='\\textbackslash{}', ['{']='\\{', ['}']='\\}',
    ['%']='\\%', ['#']='\\#', ['&']='\\&', ['_']='\\_',
    ['$']='\\$', ['^']='\\textasciicircum{}', ['~']='\\textasciitilde{}'}
  return s:gsub('[\\{}%%#&_$%^~]', map)
end
function Div(el)
  local name = el.classes[1]
  local env = envs[name]
  if not env then return el.content end
  local title = escape(el.attributes['title'] or '')
  local begin = '\\begin{' .. env .. '}'
  if env:sub(-1) == '*' or name == 'problem' then
    begin = begin .. '{' .. title .. '}'
  elseif (name == 'analysis' or name == 'supplement') and title ~= '' then
    begin = begin .. '[' .. title .. ']'
  end
  local result = {pandoc.RawBlock('latex', begin)}
  for _, block in ipairs(el.content) do table.insert(result, block) end
  table.insert(result, pandoc.RawBlock('latex', '\\end{' .. env .. '}'))
  return result
end
