#!/usr/bin/env python3
"""把 markdown 报告转成可导入 docx 的 HTML（简洁排版）。"""
import html
import pathlib
import re
import sys

CSS = """<style>
:root {
  --fs-h1: 19pt; --fs-h2: 15pt; --fs-h3: 13pt;
  --fs-body: 12pt; --fs-small: 10.5pt; --fs-code: 10pt;
  --ff-heading: "SimHei", "黑体", "Microsoft YaHei", sans-serif;
  --ff-body: "SimSun", "宋体", serif;
  --ff-mono: "Menlo", "SimSun", monospace;
  --lh-body: 1.75; --lh-heading: 1.4;
  --fw-bold: 700; --fw-normal: 400;
  --color-primary: #1B4F72; --color-text: #212121; --color-muted: #5A5A5A;
  --color-border: #C8C8C8; --color-bg: #F2F4F6; --color-highlight: #7D3C98;
  --color-callout-bg: #EDF3F8;
  --spacing-paragraph: 0.7em; --spacing-section: 1.8em; --spacing-block: 1em;
  --margin-page: 2.5cm; --page-content-width: 15.6cm;
}
body { max-width: var(--page-content-width); margin-left:auto; margin-right:auto;
  font-family: var(--ff-body); font-size: var(--fs-body); line-height: var(--lh-body); color: var(--color-text); }
p { font-family: var(--ff-body); font-size: var(--fs-body); line-height: var(--lh-body);
  color: var(--color-text); text-indent: 2em; text-align: justify; margin: var(--spacing-paragraph) 0; }
h1 { font-family: var(--ff-heading); font-size: var(--fs-h1); font-weight: var(--fw-bold);
  color: var(--color-primary); line-height: var(--lh-heading); text-indent:0; text-align:left;
  margin: var(--spacing-section) 0 var(--spacing-block) 0; }
h2 { font-family: var(--ff-heading); font-size: var(--fs-h2); font-weight: var(--fw-bold);
  color: var(--color-primary); line-height: var(--lh-heading); text-indent:0; text-align:left;
  margin: var(--spacing-block) 0 var(--spacing-paragraph) 0; }
h3 { font-family: var(--ff-heading); font-size: var(--fs-h3); font-weight: var(--fw-bold);
  color: var(--color-text); line-height: var(--lh-heading); text-indent:0; text-align:left;
  margin: var(--spacing-block) 0 var(--spacing-paragraph) 0; }
table { width:100%; border-collapse: collapse; margin: var(--spacing-block) 0;
  font-family: var(--ff-body); font-size: var(--fs-small); }
caption { font-family: var(--ff-heading); font-size: var(--fs-small); font-weight: var(--fw-bold);
  color: var(--color-muted); text-align:left; padding-bottom: 0.4em; }
th { font-family: var(--ff-heading); font-size: var(--fs-small); font-weight: var(--fw-bold);
  color: var(--color-text); background: var(--color-bg); border: 0.5pt solid var(--color-border);
  padding: 5pt 6pt; text-align:left; line-height: 1.5; }
td { font-family: var(--ff-body); font-size: var(--fs-small); color: var(--color-text);
  border: 0.5pt solid var(--color-border); padding: 5pt 6pt; text-align:left; line-height: 1.5; }
pre { font-family: var(--ff-mono); font-size: var(--fs-code); line-height: 1.5;
  background: var(--color-bg); border: 0.5pt solid var(--color-border); padding: 7pt 9pt;
  margin: var(--spacing-block) 0; white-space: pre-wrap; text-indent:0; text-align:left; }
ul, ol { font-family: var(--ff-body); font-size: var(--fs-body); line-height: var(--lh-body);
  margin: var(--spacing-paragraph) 0; padding-left: 2em; }
li { font-family: var(--ff-body); font-size: var(--fs-body); line-height: var(--lh-body);
  color: var(--color-text); margin: 0.3em 0; text-align: justify; }
strong { font-weight: var(--fw-bold); color: var(--color-text); }
blockquote { border-left: 3pt solid var(--color-primary); background: var(--color-callout-bg);
  padding: 7pt 10pt; margin: var(--spacing-block) 0; }
blockquote p { text-indent: 0; }
@page { margin: var(--margin-page); @bottom-center { content: counter(page); } }
</style>"""


def inline(text: str) -> str:
    """行内格式：粗体、行内代码。"""
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<strong>\1</strong>", t)
    return t


def convert(md_path: str, out_path: str, title: str) -> None:
    lines = pathlib.Path(md_path).read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    i, h1n = 0, 0
    while i < len(lines):
        ln = lines[i]

        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            if len(rows) >= 2:

                def cells(r: str) -> list[str]:
                    return [c.strip() for c in r.strip().strip("|").split("|")]

                head = cells(rows[0])
                body = [cells(r) for r in rows[2:]]
                out.append("<table><thead><tr>")
                out.append("".join(f"<th>{inline(h)}</th>" for h in head))
                out.append("</tr></thead><tbody>")
                for r in body:
                    out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
                out.append("</tbody></table>")
            continue

        if ln.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre>" + html.escape("\n".join(buf)) + "</pre>")
            continue

        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote><p>" + inline(" ".join(buf)) + "</p></blockquote>")
            continue

        if ln.startswith("# "):
            h1n += 1
            if h1n == 1:
                # 文档名不占标题层级（否则会被收进目录/影响 h1 计数）
                out.append('<p style="font-size:var(--fs-h2);font-weight:var(--fw-bold);'
                           'color:var(--color-primary);text-indent:0;text-align:left;'
                           'margin:0 0 var(--spacing-block) 0">'
                           + inline(ln[2:].strip()) + '</p>')
            else:
                out.append(f'<h1 id="section-{h1n - 1}">{inline(ln[2:].strip())}</h1>')
            i += 1
            continue
        if ln.startswith("## "):
            out.append(f"<h2>{inline(ln[3:].strip())}</h2>")
            i += 1
            continue
        if ln.startswith("### "):
            out.append(f"<h3>{inline(ln[4:].strip())}</h3>")
            i += 1
            continue

        if re.match(r"^\d+\. ", ln):
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                items.append(lines[i].split(". ", 1)[1])
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue

        if ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue

        if ln.strip() == "":
            i += 1
            continue

        out.append(f"<p>{inline(ln)}</p>")
        i += 1

    body = chr(10).join(out)
    body = body.replace("<p>DIVSTART_EXEC</p>", '<div class="executive-summary">')
    body = body.replace("<p>DIVEND_EXEC</p>", "</div>")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="docx-page-size" content="A4">
<title>{html.escape(title)}</title>
{CSS}
</head>
<body>
<section role="body" data-page-restart="1">
{body}
</section>
</body>
</html>"""
    pathlib.Path(out_path).write_text(html_doc, encoding="utf-8")
    print(f"已生成 {out_path}")


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "报告")
