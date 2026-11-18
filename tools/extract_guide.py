#!/usr/bin/env python3
"""지침서 PDF -> 쪽 표시가 붙은 텍스트 + 누락 점검 보고서.

사용법:
    python3 -I tools/extract_guide.py "지침서.pdf" [출력폴더] [--no-ocr]

출력 (기본 출력폴더: guides/):
    <이름>.txt        쪽마다 <<<PDF n쪽 | 인쇄 m쪽>>> 표시가 붙은 전체 텍스트.
                      그림으로 들어간 표·흐름도는 OCR 결과를 [그림·표 속 글자(OCR)] 블록으로 덧붙임
    <이름>.report.md  누락 점검 결과

동작:
  1) pdftotext로 글자 정보를 그대로 추출 (정확)
  2) 모든 쪽을 300dpi로 그려 tesseract(kor+eng) OCR
  3) OCR로 읽힌 한글이 글자 정보보다 확연히 많은 쪽 = 그림 속 글자가 있는 쪽 → OCR 결과 첨부
  4) pypdf로 한 번 더 추출해 쪽별 글자 수를 교차검증

필요 도구: poppler-utils, poppler-data(한글 글꼴 매핑), tesseract-ocr, tesseract-ocr-kor, pypdf
    apt-get install -y poppler-data tesseract-ocr tesseract-ocr-kor
"""
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

HANGUL = re.compile(r"[가-힣]")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def page_count(pdf):
    return int(re.search(r"Pages:\s+(\d+)", run(["pdfinfo", pdf]).stdout).group(1))


def extract_pages(pdf):
    r = run(["pdftotext", "-layout", pdf, "-"])
    pages = r.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    return pages, [l for l in r.stderr.splitlines() if l.strip()]


def printed_number(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for cand in (lines[-1], lines[0]) if lines else ():
        m = re.search(r"(?:^|\s)(\d{1,4})$", cand) or re.match(r"^(\d{1,4})(?:\s|$)", cand)
        if m:
            return int(m.group(1))
    return None


def ocr_page(pdf, page, tmp):
    base = os.path.join(tmp, f"p{page}")
    run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "300", "-gray", "-png", "-singlefile", pdf, base])
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    out = run(["tesseract", base + ".png", "-", "-l", "kor+eng", "--psm", "3"], env=env).stdout
    os.remove(base + ".png")
    return re.sub(r"\n\s*\n+", "\n", out).strip()


def figure_pages(pdf):
    """본문 그림(표·흐름도 등)이 있는 쪽. 여러 쪽에 반복되는 장식 이미지는 제외하고,
    3쪽 이하에만 쓰인 2제곱인치 이상의 이미지가 있는 쪽을 고른다."""
    from collections import defaultdict
    lines = run(["pdfimages", "-list", pdf]).stdout.splitlines()[2:]
    rows, pages_of = [], defaultdict(set)
    for l in lines:
        f = l.split()
        try:
            page = int(f[0])
            obj = f[10] if f[10] != "[inline]" else f"i{page}_{f[1]}"
            w_in, h_in = int(f[3]) / float(f[-4]), int(f[4]) / float(f[-3])
        except (ValueError, IndexError, ZeroDivisionError):
            continue
        rows.append((page, obj, w_in, h_in))
        pages_of[obj].add(page)
    return sorted({p for p, o, w, h in rows if len(pages_of[o]) <= 3 and w * h >= 2.0 and min(w, h) >= 0.6})


def pypdf_lengths(pdf):
    try:
        import pypdf
    except ImportError:
        return None
    out = []
    for p in pypdf.PdfReader(pdf).pages:
        try:
            out.append(len(re.sub(r"\s", "", p.extract_text() or "")))
        except Exception:
            out.append(-1)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_ocr = "--no-ocr" not in sys.argv
    pdf = args[0]
    outdir = args[1] if len(args) > 1 else "guides"
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(pdf))[0].strip("★").replace("+", " ").strip()
    n = page_count(pdf)
    pages, warnings = extract_pages(pdf)
    nums = [printed_number(p) for p in pages]
    offs = Counter(i + 1 - x for i, x in enumerate(nums) if x is not None).most_common(3)
    main_off = offs[0][0] if offs else 0

    ocr = [""] * len(pages)
    if use_ocr:
        with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(os.cpu_count() or 2) as ex:
            ocr = list(ex.map(lambda i: ocr_page(pdf, i + 1, tmp), range(len(pages))))

    h_txt = [len(HANGUL.findall(p)) for p in pages]
    h_ocr = [len(HANGUL.findall(o)) for o in ocr]
    # OCR에서만 보이는 한글이 많으면 = 그림 속 글자
    img_text = [i for i in range(len(pages)) if h_ocr[i] - h_txt[i] >= 40 and h_ocr[i] >= 1.3 * h_txt[i]]
    lens = [len(re.sub(r"\s", "", p)) for p in pages]
    # 표지·간지(글자 거의 없음)는 제외한 본문 그림 쪽
    figs = {p - 1 for p in figure_pages(pdf) if p - 1 < len(pages) and lens[p - 1] >= 30} | set(img_text)

    with open(os.path.join(outdir, base + ".txt"), "w", encoding="utf-8") as f:
        f.write(f"# {base}\n# 원본: {os.path.basename(pdf)} / 총 {n}쪽\n")
        f.write(f"# 인쇄 쪽 = PDF 쪽 - {main_off} (본문 기준)\n")
        f.write("# [그림·표 속 글자(OCR)] 블록은 자동 인식이라 오탈자가 있을 수 있음 → 숫자·금액은 원본 PDF 쪽에서 확인\n")
        f.write("# 쪽 머리에 '그림 있음'이 붙은 쪽은 표·흐름도가 그림으로 들어 있음 → 관련 질문이면 원본 쪽을 이미지로 열어 확인\n")
        for i, p in enumerate(pages):
            label = f"인쇄 {nums[i]}쪽" if nums[i] is not None else "인쇄 쪽번호 없음"
            if i in figs:
                label += " | 그림 있음"
            f.write(f"\n<<<PDF {i + 1}쪽 | {label}>>>\n{p.rstrip()}\n")
            if i in img_text:
                f.write("\n[그림·표 속 글자(OCR) 시작 — 오탈자 가능]\n" + ocr[i] + "\n[그림·표 속 글자(OCR) 끝]\n")

    alt = pypdf_lengths(pdf)
    mismatch = []
    if alt and len(alt) == len(lens):
        mismatch = [(i + 1, a, b) for i, (a, b) in enumerate(zip(lens, alt))
                    if b >= 0 and max(a, b) > 100 and abs(a - b) / max(a, b) > 0.15]
    empty = [i + 1 for i, L in enumerate(lens) if L < 30 and i not in img_text]
    broken = [i + 1 for i, p in enumerate(pages) if "�" in p]
    font_warn = sum(1 for w in warnings if "language pack" in w or "font" in w.lower())

    with open(os.path.join(outdir, base + ".report.md"), "w", encoding="utf-8") as f:
        f.write(f"# 추출 점검 보고서: {base}\n\n")
        f.write(f"- PDF 쪽수 {n} / 추출 쪽수 {len(pages)}\n")
        f.write(f"- 글자 정보로 추출한 글자 수(공백 제외): {sum(lens):,}\n")
        f.write(f"- 쪽 번호 차이(PDF-인쇄) 상위: {offs}\n")
        f.write(f"- 한글 글꼴 매핑 경고: {font_warn}건 / 깨진 글자(�) 포함 쪽: {broken}\n")
        f.write(f"- pypdf 교차검증 차이 15% 초과 쪽: {mismatch}\n")
        f.write(f"- OCR 실행: {'예' if use_ocr else '아니오'}\n")
        f.write(f"- **그림·표 속 글자를 OCR로 보완한 쪽 ({len(img_text)}쪽, PDF 쪽 번호)**: {[i + 1 for i in img_text]}\n")
        f.write(f"- **본문 그림이 있는 쪽 ({len(figs)}쪽, PDF 쪽 번호)** — 그림 속 내용은 원본 이미지로 확인 필요: {sorted(i + 1 for i in figs)}\n")
        f.write(f"- 글자가 거의 없는 쪽(표지·간지·빈 쪽 추정, 30자 미만): {empty}\n")
    print(f"{base}: {len(pages)}/{n}쪽, OCR보완 {len(img_text)}쪽, 깨짐 {len(broken)}, 불일치 {len(mismatch)}, 글꼴경고 {font_warn}")


if __name__ == "__main__":
    main()
