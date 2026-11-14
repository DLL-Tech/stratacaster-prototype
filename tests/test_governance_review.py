"""FC-03, queued governance review: offline checks of the script with a stub model (no API calls). Tracked changes are
read as if accepted, quotes are verified against the source, findings start as pending, and the spending cap stops
calls before they are made."""
import importlib.util
import sys
import zipfile
from pathlib import Path

from stratacaster.llm import LLMResult

_spec = importlib.util.spec_from_file_location(
    "governance_review", Path(__file__).resolve().parent.parent / "scripts" / "governance_review.py")
gr = importlib.util.module_from_spec(_spec)
sys.modules["governance_review"] = gr  # pydantic resolves the models' annotations through sys.modules
_spec.loader.exec_module(gr)

PRICE = {"input": 4.0, "output": 20.0}
DOC_XML = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           '<w:p><w:r><w:t>Agents provide decision support.</w:t></w:r></w:p>'
           '<w:p><w:del><w:r><w:delText>Agents approve staffing.</w:delText></w:r></w:del>'
           '<w:ins><w:r><w:t>The PMO approves staffing.</w:t></w:r></w:ins></w:p>'
           '</w:body></w:document>')


class StubReviewer:
    mode, model = "test", "stub-reviewer"

    def __init__(self, quote="The PMO approves staffing."):
        self.quote, self.tasks = quote, []

    def run(self, task, system, payload, output_model):
        self.tasks.append(task)
        if output_model is gr.Synthesis:
            out = gr.Synthesis(executive_summary="Advisory.", control_matrix=[], cross_document_conflicts=[],
                               top_risks=[])
        else:
            out = gr.DocReview(findings=[gr.Finding(criterion_id="G-02", severity="medium", quote=self.quote,
                                                    issue="No named PMO role.", recommendation="Name the role.")],
                               strengths=[])
        return LLMResult(out, self.mode, self.model, 0, 1000, 500, 0.01)


def unit(text="Agents provide decision support.\nThe PMO approves staffing.", did="V99"):
    return gr.split_parts([{"id": did, "set": "corpus", "title": "Test", "text": text}])


def test_docx_text_reads_tracked_changes_as_accepted(tmp_path):
    f = tmp_path / "t.docx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("word/document.xml", DOC_XML)
    text = gr.docx_text(f)
    assert "The PMO approves staffing." in text
    assert "Agents approve staffing." not in text


def test_long_documents_split_on_paragraphs():
    units = gr.split_parts([{"id": "V99", "set": "corpus", "title": "T", "text": "\n".join(["x" * 400] * 10)}],
                           part_tokens=250)
    assert len(units) == 5 and all(u["parts"] == 5 for u in units)


def test_findings_pending_with_verified_quotes():
    r = gr.review(StubReviewer(), unit(), max_usd=5.0, price=PRICE)
    f = r["findings"][0]
    assert f["finding_id"] == "F-V99-01" and f["disposition"] == "pending" and f["quote_verified"]
    assert r["synthesis"]["executive_summary"] == "Advisory." and r["not_reviewed"] == []


def test_unverifiable_quote_is_flagged():
    r = gr.review(StubReviewer(quote="Agents approve staffing."), unit(), max_usd=5.0, price=PRICE)
    assert r["findings"][0]["quote_verified"] is False


def test_spending_cap_stops_before_calling():
    llm = StubReviewer()
    r = gr.review(llm, unit(), max_usd=0.0001, price=PRICE)
    assert llm.tasks == [] and r["stopped"] and r["not_reviewed"] == ["V99"]


def test_plan_counts_calls_and_writes_nothing():
    p = gr.plan(unit() + unit(did="V98"), PRICE)
    assert p["calls"] == 3 and p["est_cost_usd"] > 0
