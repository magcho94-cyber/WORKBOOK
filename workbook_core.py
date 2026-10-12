"""Deterministic validation and PDF layout for the Jongro M workbook."""
from __future__ import annotations

import hashlib
import io
import json
import random
import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, Image, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer,
)

BASE = Path(__file__).resolve().parent
W, H = A4
PAGE_WIDTH = W - 88
BLUE = colors.HexColor("#164B70")
PINK = colors.HexColor("#B74479")
MUTED = colors.HexColor("#5D6D7A")


def _font_setup():
    if "Jongro" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Jongro", str(BASE / "assets" / "NanumGothic.ttf")))
    if "JongroSerif" not in pdfmetrics.getRegisteredFontNames():
        path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf")
        pdfmetrics.registerFont(TTFont("JongroSerif", str(path if path.exists() else BASE / "assets" / "NanumGothic.ttf")))


def source_from_text(title: str, english: str, korean: str = "") -> dict:
    """One sentence per line; blank lines delimit paragraphs; # gives a heading."""
    def blocks(raw):
        return [[line.strip() for line in re.split(r"\r?\n", part) if line.strip()]
                for part in re.split(r"(?:\r?\n\s*){2,}", raw.strip()) if part.strip()]

    english_blocks = blocks(english)
    korean_blocks = blocks(korean) if korean.strip() else []
    if not english_blocks:
        raise ValueError("영어 문장을 입력하세요. 한 줄에 한 문장씩 넣어 주세요.")
    if korean_blocks and len(korean_blocks) != len(english_blocks):
        raise ValueError("영어와 한국어의 문단 수가 다릅니다.")
    result = []
    for i, lines in enumerate(english_blocks):
        heading = ""
        if lines[0].startswith("# "):
            heading = lines.pop(0)[2:]
        if not lines:
            raise ValueError(f"Paragraph {i + 1}에 문장이 없습니다.")
        ko_lines = list(korean_blocks[i]) if korean_blocks else []
        if ko_lines and ko_lines[0].startswith("# "):
            ko_lines.pop(0)
        if ko_lines and len(ko_lines) != len(lines):
            raise ValueError(f"Paragraph {i + 1}: 영어 {len(lines)}문장, 한국어 {len(ko_lines)}문장입니다.")
        result.append({"heading": heading, "sentences": [
            {"en": en, "ko": ko_lines[j] if ko_lines else ""} for j, en in enumerate(lines)
        ]})
    return {"title": title.strip() or "English Reading Workbook", "paragraphs": result}


def _matches(sentence: str, word: str) -> list:
    return list(re.finditer(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence))


def validate(draft: dict) -> int:
    if not isinstance(draft, dict) or not draft.get("title") or not draft.get("paragraphs"):
        raise ValueError("제목과 문단이 필요합니다.")
    total = 0
    for pi, par in enumerate(draft["paragraphs"], 1):
        sentences = par.get("sentences", [])
        if not sentences:
            raise ValueError(f"Paragraph {pi}에 문장이 없습니다.")
        for si, row in enumerate(sentences, 1):
            label = f"Paragraph {pi}, 문장 {si}"
            en = row.get("en", "")
            if not en.strip() or not row.get("ko", "").strip():
                raise ValueError(f"{label}: 영어 문장과 해석이 필요합니다.")
            blanks = row.get("blanks", [])
            if len(blanks) not in (2, 3) or len(set(blanks)) != len(blanks):
                raise ValueError(f"{label}: 서로 다른 단어 빈칸 2~3개가 필요합니다.")
            masked = en
            for word in blanks:
                if not word or " " in word or not _matches(masked, word):
                    raise ValueError(f"{label}: 빈칸 단어 {word!r}가 원문에 없습니다.")
                masked = re.sub(r"(?<!\w)" + re.escape(word) + r"(?!\w)", "________", masked, count=1)
            correct = row.get("grammar_correct", "")
            wrong = row.get("grammar_wrong", "")
            if not correct or not wrong or correct == wrong or not _matches(en, correct):
                raise ValueError(f"{label}: 어법 선택지의 정답이 원문에 있는지 확인하세요.")
            if not row.get("grammar_reason", "").strip():
                raise ValueError(f"{label}: 어법 근거가 비었습니다.")
            if si > 1 and not row.get("connection", "").strip():
                raise ValueError(f"{label}: 앞 문장과의 논리 관계가 비었습니다.")
            total += 1
        if not par.get("topic", "").strip() or not par.get("paragraph_title", "").strip():
            raise ValueError(f"Paragraph {pi}: 주제와 제목이 필요합니다.")
        if not par.get("questions") or not par.get("ox"):
            raise ValueError(f"Paragraph {pi}: 내용 확인과 O/X가 필요합니다.")
        for item in par["questions"]:
            if not item.get("question") or not item.get("answer"):
                raise ValueError(f"Paragraph {pi}: 내용 확인 문항과 답을 확인하세요.")
        for item in par["ox"]:
            if not item.get("statement") or item.get("answer") not in ("O", "X") or not item.get("reason"):
                raise ValueError(f"Paragraph {pi}: O/X 문항의 주장, 정답, 근거를 확인하세요.")
    return total


def _styles():
    return {
        "title": ParagraphStyle("title", fontName="Jongro", fontSize=18, leading=25, spaceAfter=12, textColor=BLUE),
        "sub": ParagraphStyle("sub", fontName="Jongro", fontSize=11, leading=18, spaceBefore=13, spaceAfter=9, textColor=BLUE),
        "ko": ParagraphStyle("ko", fontName="Jongro", fontSize=9.5, leading=16, spaceAfter=7),
        "en": ParagraphStyle("en", fontName="JongroSerif", fontSize=10.5, leading=18, spaceAfter=8),
        "reading": ParagraphStyle("reading", fontName="JongroSerif", fontSize=11.2, leading=20, spaceAfter=8),
        "small": ParagraphStyle("small", fontName="Jongro", fontSize=8.5, leading=13, spaceAfter=8, textColor=MUTED),
        "tag": ParagraphStyle("tag", fontName="Jongro", fontSize=8, leading=11, spaceAfter=6, textColor=PINK),
    }


class Lines(Flowable):
    def __init__(self, number=2, gap=21):
        super().__init__()
        self.number, self.gap = number, gap
        self.width, self.height = PAGE_WIDTH, number * gap

    def draw(self):
        self.canv.setStrokeColor(colors.HexColor("#C9D4DD"))
        self.canv.setLineWidth(.4)
        for i in range(self.number):
            y = self.height - (i + 1) * self.gap + 3
            self.canv.line(0, y, self.width, y)


def _replace_once(text: str, word: str, replacement: str) -> str:
    result, count = re.subn(r"(?<!\w)" + re.escape(word) + r"(?!\w)",
                            lambda _: replacement, text, count=1)
    if count != 1:
        raise ValueError(f"원문에서 {word!r}을 찾지 못했습니다.")
    return result


def _order(items: list, seed: int) -> list[int]:
    order = list(range(len(items)))
    random.Random(seed).shuffle(order)
    if len(order) > 1 and order == list(range(len(items))):
        order = order[1:] + order[:1]
    return order


def _labels(length):
    return [chr(65 + i) if i < 26 else f"{i + 1}" for i in range(length)]


def reading_ranges(count: int) -> list[tuple[int, int]]:
    """Group a paragraph into pages of 2 or 3, with no orphaned final sentence."""
    if count < 1:
        raise ValueError("문단에 최소 한 문장이 필요합니다.")
    sizes = ([3] if count >= 3 and count % 2 else []) + [2] * ((count - (3 if count >= 3 and count % 2 else 0)) // 2)
    if count == 1:
        sizes = [1]
    ranges = []
    start = 0
    for size in sizes:
        ranges.append((start, start + size))
        start += size
    assert start == count
    return ranges


def _paragraph_chunks(draft):
    paragraphs = draft["paragraphs"]
    if len(paragraphs) > 1:
        return [[row["en"] for row in par["sentences"]] for par in paragraphs]
    rows = [row["en"] for row in paragraphs[0]["sentences"]]
    if len(rows) < 2:
        return [rows]
    count = min(3, len(rows))
    return [rows[(len(rows)*i)//count:(len(rows)*(i+1))//count] for i in range(count)]


def _decorate(canvas, doc, title: str, logo: Path | None):
    canvas.saveState()
    if logo and logo.exists():
        canvas.drawImage(str(logo), 44, H - 66, width=38, height=38, preserveAspectRatio=True, mask="auto")
    canvas.setFont("Jongro", 9.5)
    canvas.setFillColor(BLUE)
    canvas.drawString(92, H - 43, "종로M ENGLISH")
    canvas.setFont("Jongro", 8)
    canvas.drawRightString(W - 44, H - 43, title[:42])
    canvas.setStrokeColor(BLUE)
    canvas.line(44, H - 74, W - 44, H - 74)
    canvas.setFillColor(MUTED)
    canvas.drawString(44, 28, "전체 문장 워크북")
    canvas.drawRightString(W - 44, 28, str(doc.page))
    canvas.restoreState()


def _document(path: Path, title: str, logo: Path | None):
    doc = BaseDocTemplate(str(path), pagesize=A4, leftMargin=44, rightMargin=44,
                          topMargin=90, bottomMargin=50, title=title, author="종로M ENGLISH")
    frame = Frame(44, 50, PAGE_WIDTH, H - 140, id="content", leftPadding=0,
                  rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id="standard", frames=frame,
                                      onPage=lambda c, d: _decorate(c, d, title, logo)))
    return doc


def make_pdfs(draft: dict, out_dir: Path, illustration_dir: Path | None = None) -> tuple[Path, Path, dict]:
    """Render reviewed data. The source English is never rewritten by this function."""
    _font_setup()
    total = validate(draft)
    out_dir.mkdir(parents=True, exist_ok=True)
    logo = BASE / "assets" / "jongrom-logo.png"
    styles = _styles()
    p = lambda value, style="ko": Paragraph(escape(str(value)).replace("\n", "<br/>"), styles[style])
    student, teacher = out_dir / "학생용_워크북.pdf", out_dir / "교사용_정답.pdf"
    story, answers = [], []
    seen = {key: [] for key in ("reading", "step1", "step2", "step3", "step4", "step5")}
    ids = [(i + 1, j + 1) for i, par in enumerate(draft["paragraphs"]) for j in range(len(par["sentences"]))]

    def heading(title, desc=""):
        if story:
            story.append(PageBreak())
        story.append(p(title, "title"))
        if desc:
            story.append(p(desc, "small"))

    heading(draft["title"], "종로M ENGLISH · 전체 문장 단계별 워크북")
    story.extend([p("이름 ____________________    반 __________    날짜 __________"), Spacer(1, 20)])
    for line in ["READING · 원문과 해석, 필기, 문단별 주제·제목", "STEP 1 · 해석 / 문장 사이의 논리 관계 / 내용 확인 / O/X",
                 "STEP 2 · 모든 문장 빈칸 2~3개", "STEP 3 · 모든 문장 어법 선택과 근거 두 줄",
                 "STEP 4 · 문단 순서 배열", "STEP 5 · 문장 순서 배열"]:
        story.append(p(line, "sub"))
    story.append(p(f"총 {total}문장 · {len(draft['paragraphs'])}개 Paragraph", "small"))

    for pi, par in enumerate(draft["paragraphs"], 1):
        rows = par["sentences"]
        ranges = reading_ranges(len(rows))
        for group_i, (begin, end) in enumerate(ranges, 1):
            heading(f"READING · Paragraph {pi} ({group_i}/{len(ranges)})", par.get("heading", ""))
            pic = illustration_dir / f"p{pi}_g{group_i}.png" if illustration_dir else None
            if pic and pic.exists():
                from reportlab.lib.utils import ImageReader
                iw, ih = ImageReader(str(pic)).getSize()
                box_w, box_h = PAGE_WIDTH, 135 if end - begin == 3 else 190
                scale = min(box_w / iw, box_h / ih)
                story.extend([Image(str(pic), width=iw*scale, height=ih*scale), Spacer(1, 10)])
            if end - begin == 3:
                note_gap, after_gap = (14, 12) if pic and pic.exists() else (30, 24)
            else:
                note_gap, after_gap = (24, 20) if pic and pic.exists() else (54, 42)
            for j in range(begin, end):
                row, ident = rows[j], (pi, j+1)
                seen["reading"].append(ident)
                story.extend([p(f"{pi}-{j+1:02d}", "tag"), p(row["en"], "reading"), Spacer(1, note_gap),
                              p(row["ko"]), Spacer(1, after_gap)])
            if end == len(rows):
                story.extend([p("주제 (Topic)", "sub"), Lines(1, 24),
                              p("제목 (Title)", "sub"), Lines(1, 24)])

    for pi, par in enumerate(draft["paragraphs"], 1):
        heading(f"STEP 1 · 전체 문장 해석 | Paragraph {pi}", "A. 모든 문장을 우리말로 해석하세요.")
        for j, row in enumerate(par["sentences"], 1):
            seen["step1"].append((pi, j))
            story.append(KeepTogether([p(f"{pi}-{j:02d}", "tag"), p(row["en"], "en"), Lines(2), Spacer(1, 12)]))
        heading(f"STEP 1 · 문장 사이의 논리 관계 파악 | Paragraph {pi}",
                "B. 앞 문장 다음에 이 문장이 이어지는 이유를 간략히 쓰세요.")
        for j, row in enumerate(par["sentences"], 1):
            if j > 1:
                story.extend([p(f"↓ {pi}-{j-1:02d} → {pi}-{j:02d}", "tag"), Lines(2, 21), Spacer(1, 8)])
            story.extend([p(f"{pi}-{j:02d}", "tag"), p(row["en"], "en"), Spacer(1, 8)])
        heading(f"STEP 1 · 내용 확인 | Paragraph {pi}", "C. 큰 흐름부터 세부 사실까지 우리말로 답하세요.")
        for j, qa in enumerate(par["questions"], 1):
            story.extend([p(f"{j:02d}. {qa['question']}"), Lines(2), Spacer(1, 11)])
        heading(f"STEP 1 · 내용 확인 O/X | Paragraph {pi}", "D. 문장 전체가 맞으면 O, 틀리면 X를 고르세요.")
        for j, ox in enumerate(par["ox"], 1):
            story.extend([p(f"{j:02d}. ( O / X )  {ox['statement']}"), Spacer(1, 22)])

    for pi, par in enumerate(draft["paragraphs"], 1):
        heading(f"STEP 2 · 핵심 표현 빈칸 | Paragraph {pi}", "각 문장에 빈칸 2~3개가 있습니다. 빈칸 하나에 단어 하나를 쓰세요.")
        for j, row in enumerate(par["sentences"], 1):
            seen["step2"].append((pi, j))
            sentence = row["en"]
            for word in row["blanks"]:
                sentence = _replace_once(sentence, word, "________")
            story.append(KeepTogether([p(f"{pi}-{j:02d}", "tag"), p(row["ko"]), p(sentence, "en"), Spacer(1, 12)]))

    for pi, par in enumerate(draft["paragraphs"], 1):
        heading(f"STEP 3 · 어법 선택 | Paragraph {pi}", "원문과 문법에 맞는 표현을 선택하고, 아래 두 줄에 근거를 쓰세요.")
        for j, row in enumerate(par["sentences"], 1):
            seen["step3"].append((pi, j))
            pair = [row["grammar_correct"], row["grammar_wrong"]]
            random.Random(pi*10000+j).shuffle(pair)
            sentence = _replace_once(row["en"], row["grammar_correct"], "["+" / ".join(pair)+"]")
            story.append(KeepTogether([p(f"{pi}-{j:02d}", "tag"), p(sentence, "en"), Lines(2, 23), Spacer(1, 12)]))

    chunks = _paragraph_chunks(draft)
    order = _order(chunks, 20261011)
    labels = _labels(len(chunks))
    heading("STEP 4 · 문단 순서 배열", "모든 본문 부분을 읽고 원래 순서대로 배열하세요.")
    for pos, original in enumerate(order):
        story.extend([p(f"({labels[pos]})", "sub"), p(" ".join(chunks[original]), "en"), Spacer(1, 15)])
    seen["step4"] = ids.copy()
    story.extend([p("정답 순서", "sub"), Lines(1, 25)])
    paragraph_answer = " → ".join(labels[order.index(i)] for i in range(len(chunks)))

    sentence_answers = []
    for pi, par in enumerate(draft["paragraphs"], 1):
        rows = par["sentences"]
        order2 = _order(rows, 20261011 + pi)
        labels2 = _labels(len(rows))
        heading(f"STEP 5 · 문장 순서 배열 | Paragraph {pi}",
                "모든 문장을 한 번씩 사용하여 원문 순서대로 배열하세요.")
        for pos, original in enumerate(order2):
            seen["step5"].append((pi, original+1))
            story.extend([p(f"({labels2[pos]})  {rows[original]['en']}", "en"), Spacer(1, 8)])
        story.extend([p("정답 순서", "sub"), Lines(1, 25)])
        sentence_answers.append(" → ".join(labels2[order2.index(i)] for i in range(len(rows))))

    for step, counted in seen.items():
        if sorted(counted) != sorted(ids):
            raise AssertionError(f"{step}의 문장 수 검증 실패: {len(counted)} / {len(ids)}")
    _document(student, draft["title"] + " | 학생용", logo).build(story)

    def key_heading(title):
        if answers:
            answers.append(PageBreak())
        answers.append(p(title, "title"))

    key_heading(draft["title"] + " · 교사용")
    answers.append(p(f"전체 {total}문장 · 학생용의 READING 및 STEP 1~5 수록 문장 검증 완료"))
    for pi, par in enumerate(draft["paragraphs"], 1):
        key_heading(f"Paragraph {pi} · 해석과 문장 연결")
        answers.extend([p("주제: " + par["topic"]), p("제목: " + par["paragraph_title"])])
        for j, row in enumerate(par["sentences"], 1):
            block = [p(f"{pi}-{j:02d}  {row['en']}", "en"), p("해석: " + row["ko"])]
            if j > 1:
                block.append(p(f"{pi}-{j-1:02d} → {pi}-{j:02d}: {row['connection']}", "small"))
            answers.append(KeepTogether(block))
        key_heading(f"Paragraph {pi} · 내용 확인 / O/X")
        for j, qa in enumerate(par["questions"], 1):
            answers.extend([p(f"{j:02d}. {qa['question']}"), p("예시 답: " + qa["answer"], "small")])
        for j, ox in enumerate(par["ox"], 1):
            answers.extend([p(f"{j:02d}. {ox['statement']}"),
                            p(f"{ox['answer']} · {ox['reason']}", "small")])
        key_heading(f"Paragraph {pi} · STEP 2·3 정답")
        for j, row in enumerate(par["sentences"], 1):
            answers.extend([p(f"{pi}-{j:02d}  빈칸: {' / '.join(row['blanks'])}"),
                            p(f"어법: {row['grammar_correct']} | {row['grammar_reason']}"), Spacer(1, 6)])
    key_heading("STEP 4·5 · 배열 정답")
    answers.append(p("STEP 4: " + paragraph_answer))
    for pi, answer in enumerate(sentence_answers, 1):
        answers.append(p(f"STEP 5 · Paragraph {pi}: {answer}"))
    _document(teacher, draft["title"] + " | 교사용", logo).build(answers)
    return student, teacher, {"paragraphs": len(draft["paragraphs"]), "sentences": total,
                              "coverage": {k: len(v) for k, v in seen.items()},
                              "step4_answer": paragraph_answer, "step5_answers": sentence_answers}
