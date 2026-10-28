"""The rule that finds printed quantities in a parsed block (2026-10-18): the one source of it for the recording (quantity_marks.py) and
the measurement. It is the rule that drafted the reviewed fact sheets, moved here unchanged from
fullscore-handoff-4papers-20261011/tools/build_review_pack.py, which keeps its own frozen copy because the sheets it drafted are history;
tests/test_quantity_rule.py checks that the two find the same quantities in every parsed block of the fixtures. No model is called here."""
import re

_TEX = [(r'\\(?:text|mathrm|mathbf|textbf|mathit|operatorname)\s*\{([^{}]*)\}', r'\1'), (r'</?(?:sup|sub|b|i|em|strong)>', ''), (r'\^\s*\{?\s*(?:\\circ|o)\s*\}?\s*(?:\\mathrm\s*)?C', '°C'), (r'\^\s*\{?\s*\\circ\s*\}?', '°'),
        (r'\\mu\s*', 'µ'), (r'\\%', '%'), (r'\\times', '×'), (r'\\[,;! ]', ' '), (r'[$]', ''), (r'[{}]', ''), (r'μ', 'µ'), (r'[−–]', '-'), (r'~', ' ')]


def plain(text):
    for _ in range(3): text = re.sub(_TEX[0][0], _TEX[0][1], text)
    for pattern, to in _TEX[1:]: text = re.sub(pattern, to, text)
    return re.sub(r'[ \t]{2,}', ' ', text).replace('° C', '°C')


def text_of(row):
    # The parse sometimes hangs a whole paragraph on a figure as its caption (a procedure printed under its scheme): that is entry text.
    return plain('\n'.join([(row.get('raw_text') or '').strip(), *[c for c in (row.get('captions') or []) if len(c) > 200 or row['type'] == 'table']]).strip())


NUMBER = r'(?<![\w.])(-\s?)?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\d,])'
UNITS = r'(mmol|µmol|nmol|mol%|mol|mL|µL|L|mg|µg|kg|g|°C|%|equiv\.?|eq\.?|M|mM|µM|N|h|min|s|d|hours?|minutes?|seconds?|days?|drops?|mbar|bar|atm|psi|nm|W|rpm|wt%)'
QUANTITY = re.compile(NUMBER + r'\s?' + UNITS + r'(?![A-Za-z])')
WORDS = re.compile(r'\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty|thirty|a few|several|an?|another|additional)\s+(hours?|minutes?|days?|drops?|times|portions?)\b|\b(overnight|room temperature|rt|r\.t\.|reflux|ambient temperature|twice|thrice)\b', re.I)
DATA = re.compile(r'(?:R\W*f\W*=?\W*0\W*\.|\[\W*(?:α|alpha)\W*\]|(?:1\W*H|13\W*C|19\W*F|31\W*P)\W{0,6}NMR|\bIR\W*\(|HRMS|HR-?ESI|\bm\.?p\.?\W|melting point)', re.I)
