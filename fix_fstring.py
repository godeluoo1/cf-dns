import re
with open('cf.py', 'r', encoding='utf-8') as f:
    content = f.read()

# I need to escape the JS script I injected.
# The injected script starts with <script>\nasync function pollUpdate() {
# and ends with setTimeout(pollUpdate, 60000);\n</script>\n</body>
import sys

start_idx = content.find('<script>\nasync function pollUpdate()')
end_idx = content.find('</body>', start_idx)
if start_idx == -1 or end_idx == -1:
    sys.exit("Could not find script block")

script_block = content[start_idx:end_idx]

# Fix the { and }
# Wait, I previously injected it, so it's already there with single { and }.
# Let's replace single { with {{ and single } with }}
# BUT be careful not to replace already double {{.
# Actually, it's easier to just do script_block.replace('{', '{{').replace('}', '}}') 
# because I wrote the script block myself and know there are no double {{.
# Wait, there's a template literal inside the JS: .line-row[data-sub="${sub}"][data-line="${line_key}"]
# If I do replace('{', '{{'), it becomes $...
# Actually in f-string, you can't have expressions inside {{...}}.
# So `data-sub="${{sub}}"` in JS becomes `data-sub="${sub}"` in output HTML.
# That is exactly what JS needs!
# Wait, Python f-string evaluates {sub} as a python variable!
# I don't want python to evaluate sub. I want JS to evaluate it!
# So I should write it as `${{sub}}` so Python outputs `${sub}`.

fixed_script = script_block.replace('{', '{{').replace('}', '}}')
# Wait, JS template literal has `${{sub}}`. When I do replace, it becomes `${{{{sub}}}}`
# I wrote: .line-row[data-sub="${sub}"][data-line="${line_key}"]
# replace -> .line-row[data-sub="${{sub}}"][data-line="${{line_key}}"]
# Then python f-string will output .line-row[data-sub="${sub}"][data-line="${line_key}"] !
# This is perfect!

content = content[:start_idx] + fixed_script + content[end_idx:]

with open('cf.py', 'w', encoding='utf-8') as f:
    f.write(content)
