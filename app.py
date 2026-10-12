"""Local browser app. Keep OPENAI_API_KEY in the server environment."""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import uuid
import urllib.error
import urllib.request
import zipfile
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from workbook_core import make_pdfs, reading_ranges, source_from_text, validate

BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "output"
MAX_BODY = 5_000_000
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _schema():
    obj = lambda properties: {"type": "object", "properties": properties,
                              "required": list(properties), "additionalProperties": False}
    string = {"type": "string"}
    return obj({
        "sentences": {"type": "array", "items": obj({
            "translation": string, "blanks": {"type": "array", "items": string},
            "grammar_correct": string, "grammar_wrong": string,
            "grammar_reason": string, "connection": string,
        })},
        "topic": string, "paragraph_title": string,
        "questions": {"type": "array", "items": obj({"question": string, "answer": string})},
        "ox": {"type": "array", "items": obj({"statement": string,
            "answer": {"type": "string", "enum": ["O", "X"]}, "reason": string})},
    })


def _api_post(path: str, data: dict, timeout=240) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ValueError("OPENAI_API_KEY가 설정되지 않았습니다. 안내문의 실행 방법을 확인하세요.")
    req = urllib.request.Request("https://api.openai.com/v1/" + path,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            message = json.loads(raw).get("error", {}).get("message", raw)
        except json.JSONDecodeError:
            message = raw[:500]
        raise ValueError(f"OpenAI API 오류 ({exc.code}): {message}") from exc


def _response_json(response: dict) -> dict:
    if response.get("status") != "completed":
        raise ValueError(f"모델 응답이 완료되지 않았습니다: {response.get('status')} / {response.get('incomplete_details')}")
    texts = []
    for entry in response.get("output", []):
        for block in entry.get("content", []):
            if block.get("type") == "refusal":
                raise ValueError("모델이 응답 생성을 거절했습니다: " + block.get("refusal", ""))
            if block.get("type") == "output_text":
                texts.append(block["text"])
    if not texts:
        raise ValueError("모델 응답에 텍스트가 없습니다.")
    return json.loads("".join(texts))


def _generate_one(par: dict, index: int, model: str, feedback: str = "") -> dict:
    count = len(par["sentences"])
    target = min(4, max(2, count))
    instruction = (
        "You design a Korean high-school English workbook. Return the structured JSON only. "
        "Use the input sentences in exact order. Produce exactly one sentence object per input sentence. "
        "For each: natural Korean translation (use provided Korean translation verbatim if nonempty); "
        "2 or 3 DISTINCT single-word blanks copied EXACTLY from that English sentence, spanning useful meanings/grammar; "
        "one grammar_correct substring copied EXACTLY from the original sentence, and one clearly "
        "grammatically wrong alternative; Korean grammar_reason. Make choices genuinely distinct and "
        "unambiguous; do not use a mere stylistic preference. The connection explains briefly, in Korean, "
        "WHY the previous sentence logically leads to the current sentence; for first sentence use empty string. "
        f"Write {target} Korean content questions and {target} tricky but fair Korean O/X statements "
        "with O and X both represented. Start content questions with the broad idea, progress toward "
        "specific details. An X statement must have a substantive correction in its reason. "
        "All answers must be grounded in the given paragraph. Provide topic and title in Korean. "
        "Never add source facts. For a quoted contraction choose exact single token when making blanks."
    )
    if feedback:
        instruction += " Fix these validation issues from prior attempt: " + feedback[:1000]
    response = _api_post("responses", {
        "model": model, "store": False,
        "input": [{"role": "developer", "content": instruction},
                  {"role": "user", "content": json.dumps({"paragraph": index, **par}, ensure_ascii=False)}],
        "text": {"format": {"type": "json_schema", "name": "workbook_paragraph",
                             "strict": True, "schema": _schema()}},
        "max_output_tokens": max(3500, 700*count),
    })
    result = _response_json(response)
    if len(result["sentences"]) != count:
        raise ValueError(f"문장 수가 맞지 않습니다: {len(result['sentences'])} / {count}")
    if len(result["questions"]) != target or len(result["ox"]) != target:
        raise ValueError("내용 확인 또는 O/X 문항 수가 지정한 수와 다릅니다.")
    if {item["answer"] for item in result["ox"]} != {"O", "X"}:
        raise ValueError("O/X 문항에는 O와 X가 모두 있어야 합니다.")
    return result


def create_draft(source: dict, model: str) -> dict:
    if not source.get("paragraphs"):
        raise ValueError("원문 문단을 입력하세요.")
    if not model or len(model) > 80 or not all(c.isalnum() or c in "-_." for c in model):
        raise ValueError("모델 이름이 올바르지 않습니다.")
    output = {"title": source["title"], "paragraphs": []}
    for i, par in enumerate(source["paragraphs"], 1):
        for attempt in range(2):
            try:
                result = _generate_one(par, i, model, feedback if attempt else "")
                current = {"heading": par.get("heading", ""), "topic": result["topic"],
                           "paragraph_title": result["paragraph_title"],
                           "questions": result["questions"], "ox": result["ox"],
                           "sentences": []}
                for original, suggested in zip(par["sentences"], result["sentences"]):
                    current["sentences"].append({"en": original["en"],
                        "ko": original.get("ko") or suggested["translation"],
                        "blanks": suggested["blanks"],
                        "grammar_correct": suggested["grammar_correct"],
                        "grammar_wrong": suggested["grammar_wrong"],
                        "grammar_reason": suggested["grammar_reason"],
                        "connection": suggested["connection"]})
                validate({"title": source["title"], "paragraphs": [current]})
                output["paragraphs"].append(current)
                break
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                feedback = str(exc)
                if attempt:
                    raise ValueError(f"Paragraph {i} 생성 실패: {feedback}. 입력 또는 초안을 확인하세요.") from exc
    validate(output)
    return output


def _digest(draft: dict) -> str:
    source = [[s["en"] for s in par["sentences"]] for par in draft["paragraphs"]]
    return sha256(json.dumps(["reading-groups-v2", draft["title"], source], ensure_ascii=False).encode()).hexdigest()[:12]


def illustration_groups(draft: dict):
    for pi, par in enumerate(draft["paragraphs"], 1):
        rows = par["sentences"]
        for group_i, (begin, end) in enumerate(reading_ranges(len(rows)), 1):
            yield pi, group_i, [s["en"] for s in rows[begin:end]]


def create_illustrations(draft: dict, model="gpt-image-1-mini",
                         progress: Callable[[int, int], None] | None = None) -> int:
    validate(draft)
    folder = OUTPUT / _digest(draft) / "illustrations"
    folder.mkdir(parents=True, exist_ok=True)
    count = 0
    groups = list(illustration_groups(draft))
    for done, (pi, gi, sentences) in enumerate(groups, 1):
        path = folder / f"p{pi}_g{gi}.png"
        if not path.exists() or path.stat().st_size == 0:
            prompt = (
                "A clean expressive 2D animation-style educational cartoon, simplified friendly characters, "
                "soft colors, consistent picture-book look. Show the EXACT events or concepts in these English "
                "reading sentences as one coherent scene or two or three clear panels. Avoid photorealism, avoid all text, "
                "letters, signs, captions and logos. Keep it suitable for a school worksheet. Sentences: "
                + " ".join(sentences)
            )
            result = _api_post("images/generations", {"model": model, "prompt": prompt,
                "size": "1024x1024", "quality": "low", "output_format": "png", "n": 1}, timeout=300)
            try:
                image_bytes = base64.b64decode(result["data"][0]["b64_json"], validate=True)
                if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ValueError("이미지 형식이 PNG가 아닙니다.")
                pending = path.with_suffix(".tmp")
                pending.write_bytes(image_bytes)
                pending.replace(path)
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"Paragraph {pi}의 {gi}번째 삽화 응답을 읽지 못했습니다.") from exc
            count += 1
        if progress:
            progress(done, len(groups))
    return count


def render_zip(draft: dict, include_illustrations: bool = True,
               progress: Callable[[int, int], None] | None = None) -> bytes:
    validate(draft)
    out = OUTPUT / _digest(draft)
    image_dir = out / "illustrations" if include_illustrations else None
    if include_illustrations:
        create_illustrations(draft, os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1-mini"), progress)
        if not all((image_dir / f"p{pi}_g{gi}.png").is_file()
                   for pi, gi, _ in illustration_groups(draft)):
            raise ValueError("삽화가 일부 누락되어 PDF 생성을 중단했습니다.")
    student, teacher, manifest = make_pdfs(draft, out, image_dir)
    manifest["illustrations"] = sum(1 for _ in illustration_groups(draft)) if include_illustrations else 0
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(student, "학생용_워크북.pdf")
        z.write(teacher, "교사용_정답.pdf")
        z.writestr("검토한_초안.json", json.dumps(draft, ensure_ascii=False, indent=2))
        z.writestr("문장_검증.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buffer.getvalue()


def _update_job(job_id: str, **changes):
    with JOBS_LOCK:
        JOBS[job_id].update(changes)


def start_render_job(draft: dict, include_illustrations: bool = True) -> str:
    validate(draft)
    groups = list(illustration_groups(draft)) if include_illustrations else []
    folder = OUTPUT / _digest(draft) / "illustrations"
    missing = [1 for pi, gi, _ in groups
               if not (folder / f"p{pi}_g{gi}.png").is_file()
               or (folder / f"p{pi}_g{gi}.png").stat().st_size == 0]
    if missing and not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("삽화 자동 생성에는 OPENAI_API_KEY가 필요합니다. 키를 설정하거나 '삽화 없이 시험 출력'을 선택하세요.")
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "working", "phase": "삽화 생성 준비 중" if groups else "PDF 제작 중",
                        "completed": 0, "total": len(groups), "payload": None, "error": ""}
    threading.Thread(target=_run_render_job, args=(job_id, draft, include_illustrations), daemon=True).start()
    return job_id


def _run_render_job(job_id: str, draft: dict, include_illustrations: bool):
    try:
        def report(done: int, total: int):
            phase = "PDF 제작 중" if done == total else f"삽화 준비 {done}/{total}장"
            _update_job(job_id, completed=done, total=total, phase=phase)
        payload = render_zip(draft, include_illustrations, report)
        _update_job(job_id, status="done", phase="다운로드 준비 완료", payload=payload)
    except Exception as exc:
        print("Render job failed:", repr(exc), file=sys.stderr)
        _update_job(job_id, status="error", phase="생성 실패", error=str(exc))


def get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        data = JOBS.get(job_id)
        return {k: v for k, v in data.items() if k != "payload"} if data else None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._send((BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/example":
            self._send((BASE / "example.json").read_bytes(), "application/json; charset=utf-8")
        elif self.path == "/health":
            self._json({"ok": True, "api_key_ready": bool(os.environ.get("OPENAI_API_KEY"))})
        elif self.path.startswith("/jobs/"):
            parts = self.path.strip("/").split("/")
            job_id = parts[1] if len(parts) >= 2 else ""
            job = get_job(job_id)
            if not job:
                self.send_error(404)
            elif len(parts) == 2:
                self._json(job)
            elif len(parts) == 3 and parts[2] == "download" and job["status"] == "done":
                with JOBS_LOCK:
                    payload = JOBS[job_id]["payload"]
                self._send(payload, "application/zip", "attachment; filename=JongroM_Workbook.zip")
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("입력 크기가 너무 크거나 비어 있습니다.")
            data = json.loads(self.rfile.read(length))
            if self.path == "/draft":
                source = source_from_text(data.get("title", ""), data.get("english", ""), data.get("korean", ""))
                self._json(create_draft(source, data.get("model", "gpt-5-mini")))
            elif self.path == "/start-render":
                if not isinstance(data, dict) or not isinstance(data.get("include_illustrations", True), bool):
                    raise ValueError("출력 옵션을 확인하세요.")
                job_id = start_render_job(data["draft"], data.get("include_illustrations", True))
                self._json({"job_id": job_id})
            elif self.path == "/render":
                self._send(render_zip(data), "application/zip", "attachment; filename=JongroM_Workbook.zip")
            else:
                self.send_error(404)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            print("Server error:", repr(exc), file=sys.stderr)
            self._json({"error": f"처리 중 오류가 발생했습니다: {exc}"}, 500)

    def _json(self, value, status=200):
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def _send(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("WORKBOOK_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"종로M 워크북 제작기: http://127.0.0.1:{port}")
    print("OpenAI key:", "ready" if os.environ.get("OPENAI_API_KEY") else "not set (example mode works)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
