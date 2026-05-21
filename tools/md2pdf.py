#!/usr/bin/env python3
"""Minimal Markdown -> LaTeX -> PDF (pdflatex) converter tuned for the AMI docs.
Handles headings, wrapping page-breakable tables (ltablex), fenced code,
bold/inline-code, lists, and sanitises emoji/arrows that pdflatex can't render.

Usage: python tools/md2pdf.py docs/FILE.md   ->  docs/FILE.pdf
"""
import re, sys, subprocess, os

UNI = {
    # plain-ASCII (NOT LaTeX math) — these are replaced before esc(), so any
    # backslash/$ would be re-escaped into garbage. Keep them text.
    '→': ' -> ', '←': ' <- ', '↔': ' <-> ',
    '≥': '>=', '≤': '<=', '×': 'x', '·': '-',
    '…': '...', '–': '--', '—': '---', '•': '-',
    '“': '``', '”': "''", '‘': '`', '’': "'",
    '✅': '[OK]', '❌': '[X]', '⚠': '(!)', '️': '',
    '\U0001f3af': '', '\U0001f511': '*', '\U0001f4c4': '', '\U0001f52c': '',
    '⏳': '', '\U0001f7e2': '', '\U0001f534': '', '\U0001f7e1': '',
}
SPECIAL = {'&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#', '_': r'\_',
           '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}

def sanitize_unicode(s):
    for k, v in UNI.items():
        s = s.replace(k, v)
    # drop any remaining non-Latin-Extended char (emoji etc.)
    return ''.join(c if ord(c) <= 0x2bf else '' for c in s)

# listings can't take UTF-8 bytes -> render code blocks as pure ASCII
_ACCENT = {'á':'a','é':'e','í':'i','ó':'o','ú':'u','ñ':'n','ü':'u',
           'Á':'A','É':'E','Í':'I','Ó':'O','Ú':'U','Ñ':'N','Ü':'U','¿':'?','¡':'!'}
_CODEUNI = {'→':'->','←':'<-','↔':'<->','≥':'>=','≤':'<=','×':'x','·':'-',
            '—':'--','–':'-','“':'"','”':'"','‘':"'",'’':"'",'…':'...','•':'-'}
def sanitize_code(s):
    for k, v in {**_CODEUNI, **_ACCENT}.items():
        s = s.replace(k, v)
    return ''.join(c if ord(c) < 128 else '' for c in s)

def esc(s):
    s = s.replace('\\', r'\textbackslash{}')
    for k, v in SPECIAL.items():
        s = s.replace(k, v)
    return s

def _breakable(s):
    """Allow line breaks inside long monospace tokens (paths, hex, IDENTIFIERS)
    so they don't overflow narrow columns / lines."""
    s = s.replace(r'\_', r'\_\allowbreak{}')
    for ch in '/.:;,-':
        s = s.replace(ch, ch + r'\allowbreak{}')
    return s

def inline(s):
    """Process inline code, bold, links; escape the rest."""
    out, i, ph = [], 0, []
    # protect inline code `...`
    def repl_code(m):
        ph.append(r'\texttt{' + _breakable(esc(m.group(1))) + '}')
        return f'\x00{len(ph)-1}\x00'
    s = re.sub(r'`([^`]+)`', repl_code, s)
    # links [text](url) -> \href{url}{text}
    def repl_link(m):
        ph.append(r'\href{' + m.group(2).replace('%', r'\%').replace('#', r'\#') +
                  '}{' + esc(m.group(1)) + '}')
        return f'\x00{len(ph)-1}\x00'
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', repl_link, s)
    # bold **...**
    parts = re.split(r'\*\*(.+?)\*\*', s)
    buf = ''
    for idx, p in enumerate(parts):
        buf += (r'\textbf{' + esc(p) + '}') if idx % 2 else esc(p)
    s = buf
    # restore placeholders
    for idx, val in enumerate(ph):
        s = s.replace(esc(f'\x00{idx}\x00').replace(r'\textbackslash{}',''), val)
        s = s.replace(f'\x00{idx}\x00', val)
    return s

def convert(md):
    out = []
    lines = md.split('\n')
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        # code fence
        if ln.strip().startswith('```'):
            out.append(r'\begin{lstlisting}')
            i += 1
            while i < n and not lines[i].strip().startswith('```'):
                out.append(sanitize_code(lines[i]))
                i += 1
            out.append(r'\end{lstlisting}')
            i += 1
            continue
        # table: a line with | and the next line is the |---| separator
        if ln.lstrip().startswith('|') and i + 1 < n and re.match(r'^\s*\|?[\s:|-]+\|[\s:|-]*$', lines[i+1]):
            header = [c.strip() for c in ln.strip().strip('|').split('|')]
            ncol = len(header)
            i += 2
            body = []
            while i < n and lines[i].lstrip().startswith('|'):
                body.append([c.strip() for c in lines[i].strip().strip('|').split('|')])
                i += 1
            # weighted X-columns: width proportional to (clamped) max content
            # length per column, so "#"/"COM" stay narrow and descriptions get
            # the room. The hsize weights must sum to ncol for tabularx.
            def _vislen(c):
                return len(re.sub(r'[*`]', '', c))
            maxlen = []
            for j in range(ncol):
                cells = [header[j]] + [r[j] for r in body if j < len(r)]
                maxlen.append(max((_vislen(c) for c in cells), default=1))
            clamp = [min(max(m, 5), 55) for m in maxlen]
            tot = sum(clamp) or 1
            w = [max(0.45, min(2.8, c / tot * ncol)) for c in clamp]
            sw = sum(w)
            w = [x / sw * ncol for x in w]
            w[-1] = ncol - sum(w[:-1])  # exact sum = ncol
            colspec = '|' + ''.join(
                r'>{\hsize=%.4f\hsize\raggedright\arraybackslash}X|' % x for x in w)
            fs = r'\footnotesize' if ncol >= 4 else r'\small'
            out.append('{' + fs)
            out.append(r'\begin{tabularx}{\linewidth}{' + colspec + '}')
            out.append(r'\hline')
            out.append(' & '.join(r'\textbf{' + inline(sanitize_unicode(h)) + '}' for h in header) + r' \\ \hline')
            for row in body:
                row = (row + [''] * ncol)[:ncol]
                out.append(' & '.join(inline(sanitize_unicode(c)) for c in row) + r' \\ \hline')
            out.append(r'\end{tabularx}}')
            out.append(r'\vspace{5pt}')
            continue
        s = sanitize_unicode(ln)
        st = s.strip()
        if not st:
            out.append('')
        elif st.startswith('#'):
            m = re.match(r'(#+)\s*(.*)', st)
            lvl, txt = len(m.group(1)), inline(m.group(2))
            cmd = {1: r'\section*', 2: r'\subsection*', 3: r'\subsubsection*'}.get(lvl, r'\paragraph*')
            out.append(cmd + '{' + txt + '}')
        elif re.match(r'^\s*[-*]\s+', s):
            items = []
            while i < n and re.match(r'^\s*[-*]\s+', lines[i]):
                items.append(inline(sanitize_unicode(re.sub(r'^\s*[-*]\s+', '', lines[i]))))
                i += 1
            out.append(r'\begin{itemize}[leftmargin=1.4em,itemsep=1pt,topsep=2pt]')
            out += [r'\item ' + it for it in items]
            out.append(r'\end{itemize}')
            continue
        elif st.startswith('>'):
            out.append(r'\textit{' + inline(st[1:].strip()) + r'}\\')
        elif st == '---':
            out.append(r'\noindent\rule{\linewidth}{0.4pt}')
        else:
            out.append(inline(s) + r'\\')
        i += 1
    return '\n'.join(out)

PRE = r"""\documentclass[10pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[margin=1.8cm]{geometry}
\usepackage{array}
\usepackage{tabularx}
\usepackage{ltablex}
\keepXColumns
\usepackage{xcolor}
\usepackage{listings}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\usepackage{titlesec}
\renewcommand{\arraystretch}{1.25}
\definecolor{cbg}{rgb}{0.96,0.96,0.96}
\lstset{basicstyle=\ttfamily\small,breaklines=true,backgroundcolor=\color{cbg},
        frame=single,framerule=0pt,xleftmargin=4pt,columns=fullflexible,
        breakindent=0pt,keepspaces=true,aboveskip=4pt,belowskip=4pt}
\titlespacing*{\section}{0pt}{10pt}{4pt}
\titlespacing*{\subsection}{0pt}{8pt}{3pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{2pt}
\begin{document}
"""

def main():
    src = sys.argv[1]
    base = os.path.splitext(src)[0]
    md = open(src, encoding='utf-8').read()
    tex = PRE + convert(md) + '\n\\end{document}\n'
    texf = base + '.tex'
    open(texf, 'w', encoding='utf-8').write(tex)
    outdir = os.path.dirname(os.path.abspath(texf)) or '.'
    for _ in range(2):  # twice for longtable widths
        r = subprocess.run(['pdflatex', '-interaction=nonstopmode', '-halt-on-error',
                            '-output-directory', outdir, texf],
                           capture_output=True, text=True)
    if not os.path.exists(base + '.pdf'):
        sys.stderr.write(r.stdout[-3000:])
        sys.exit('pdflatex failed')
    for ext in ('.aux', '.log', '.out', '.tex'):
        try: os.remove(base + ext)
        except OSError: pass
    print('PDF ->', base + '.pdf')

if __name__ == '__main__':
    main()
