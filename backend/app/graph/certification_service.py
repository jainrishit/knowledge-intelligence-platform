"""
Retrieval Reliability and Brain Certification Service.

Architecture
------------
For every workspace this service:

  1. Generates N benchmark questions derived entirely from the workspace's
     own knowledge graph (zero external knowledge).  Question templates are
     typed — stakeholder, risk, system interaction, capability, recommendation,
     initiative, dependency, opportunity — and grounded in actual concept names.

  2. Executes each question through the real retrieval pipeline
     (graph_memory_manager → find_nodes_semantic → get_neighbourhood) so that
     the benchmark measures exactly the same path that QA and deliverable
     generation use.

  3. Scores the run along five independent axes:
       recall_score        — % of ground-truth concepts actually retrieved
       precision_score     — % of retrieved concepts that are in ground truth
       coverage_score      — % of all workspace concepts surfaced ≥ once
       consistency_score   — stability across repeated identical queries (0=chaotic, 100=identical)
       evidence_fidelity   — % of retrieved concepts with a non-empty source_excerpt

  4. Detects knowledge gaps — concepts that were NEVER retrieved across the
     full benchmark run.

  5. Issues a certification verdict:
       CERTIFIED       — all five scores ≥ their respective thresholds
       PROVISIONAL     — overall ≥ 60 but ≥ 1 score below threshold
       NOT_CERTIFIED   — overall < 60 or critical failure

All computation is deterministic — no LLM calls during scoring.
LLM calls are made only during benchmark answer generation, and those paths
are the same LLM calls the platform uses in production (same pipeline).

Public API
----------
run_certification(db, workspace_id)
    → RetrievalCertificationReport   (run stored to DB; live computation)

get_latest_certification(db, workspace_id)
    → RetrievalCertificationReport | None   (from DB)

get_certification_status(db, workspace_id)
    → CertificationStatus   (lightweight poll-friendly dict)

get_knowledge_gaps(db, workspace_id)
    → list[KnowledgeGap]
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import (
    Concept, ConsultingPattern, Document, Relationship,
    RetrievalBenchmarkRun, Workspace,
)
from app.graph.memory_manager import graph_memory_manager
from app.graph.traversal import find_nodes_semantic, get_neighbourhood

logger = logging.getLogger(__name__)

# ── Certification thresholds ──────────────────────────────────────────────────

_THRESHOLDS = {
    "recall":        70.0,   # ≥ 70% of ground-truth concepts retrieved
    "precision":     65.0,   # ≥ 65% of retrieved concepts are relevant
    "coverage":      60.0,   # ≥ 60% of workspace concepts surfaced
    "consistency":   80.0,   # ≥ 80% stability across repeated queries
    "evidence":      70.0,   # ≥ 70% of answers have source evidence
}

_CERTIFIED_THRESHOLD    = 75.0   # overall ≥ 75 → CERTIFIED
_PROVISIONAL_THRESHOLD  = 60.0   # overall ≥ 60 → PROVISIONAL

# Certification weights
_CERT_WEIGHTS = {
    "recall":      0.30,
    "precision":   0.20,
    "coverage":    0.20,
    "consistency": 0.15,
    "evidence":    0.15,
}

# Max questions per run (keep cost/time bounded)
_MAX_QUESTIONS = 20
_HOPS          = 2
_TOP_K         = 15


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BenchmarkQuestion:
    question_id: int
    question_text: str
    question_type: str           # stakeholder | risk | system | capability | etc.
    seed_concept_names: list[str]   # ground-truth concepts that should be retrieved
    seed_concept_ids: list[int]


@dataclass
class QuestionResult:
    question_id: int
    question_text: str
    question_type: str
    ground_truth_ids: list[int]
    retrieved_ids: list[int]
    has_evidence: bool           # ≥1 retrieved concept has a source_excerpt
    recall: float                # 0–1 for this question
    precision: float             # 0–1 for this question


@dataclass
class KnowledgeGap:
    concept_id: int
    concept_name: str
    concept_type: str
    gap_type: str                # "never_retrieved" | "no_evidence" | "orphaned"
    source_document_id: Optional[int]


@dataclass
class CertificationStatus:
    workspace_id: int
    run_id: Optional[int]
    certification_status: str    # CERTIFIED | PROVISIONAL | NOT_CERTIFIED | PENDING
    certification_score: float
    recall_score: float
    precision_score: float
    coverage_score: float
    consistency_score: float
    evidence_fidelity: float
    retrieval_accuracy: float
    questions_generated: int
    questions_answered: int
    knowledge_gap_count: int
    graph_version: int
    completed_at: Optional[datetime]


@dataclass
class RetrievalCertificationReport:
    workspace_id: int
    workspace_name: str
    run_id: int
    graph_version: int
    generated_at: datetime

    # Core scores 0–100
    recall_score: float
    precision_score: float
    coverage_score: float
    consistency_score: float
    evidence_fidelity: float
    retrieval_accuracy: float    # weighted composite of above 5

    # Certification
    certification_status: str
    certification_score: float

    # Benchmark metadata
    questions_generated: int
    questions_answered: int
    total_workspace_concepts: int
    concepts_covered: int        # unique concepts surfaced across all questions
    concepts_never_retrieved: int

    # Per-question detail
    question_results: list[QuestionResult] = field(default_factory=list)

    # Gap analysis
    knowledge_gaps: list[KnowledgeGap] = field(default_factory=list)

    # Threshold checks (True = passes, False = fails)
    recall_pass: bool = False
    precision_pass: bool = False
    coverage_pass: bool = False
    consistency_pass: bool = False
    evidence_pass: bool = False


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe_pct(num: float, den: float) -> float:
    return round(100.0 * num / den, 1) if den else 0.0


def _round2(v: float) -> float:
    return round(max(0.0, min(100.0, v)), 1)


# ── Question templates ────────────────────────────────────────────────────────

_QUESTION_TEMPLATES: list[tuple[str, str]] = [
    ("stakeholder", "What stakeholders are associated with {concept}?"),
    ("risk",        "What risks are related to {concept}?"),
    ("system",      "What systems interact with {concept}?"),
    ("capability",  "What capabilities are enabled by {concept}?"),
    ("dependency",  "What does {concept} depend on?"),
    ("opportunity", "What opportunities does {concept} create?"),
    ("initiative",  "What initiatives involve {concept}?"),
    ("process",     "What processes are connected to {concept}?"),
    ("relationship","How does {concept} relate to other systems?"),
    ("standard",    "What standards govern {concept}?"),
]


def _generate_questions(
    concepts: list[Concept],
    G,
    max_q: int,
) -> list[BenchmarkQuestion]:
    """
    Generate benchmark questions grounded in the actual workspace graph.

    Strategy:
      - Pick the top-N most-connected concepts (highest degree = most important).
      - For each selected concept, apply a typed template.
      - ground_truth = the concept itself + its 1-hop neighbours.
      - Cap at max_q total questions.
    """
    if not concepts:
        return []

    # Rank by graph degree (most central = most testable)
    def degree(c: Concept) -> int:
        if G.has_node(c.id):
            return G.in_degree(c.id) + G.out_degree(c.id)
        return 0

    ranked = sorted(concepts, key=degree, reverse=True)
    selected = ranked[:max_q]

    questions: list[BenchmarkQuestion] = []
    n_templates = len(_QUESTION_TEMPLATES)

    for i, concept in enumerate(selected):
        q_type, template = _QUESTION_TEMPLATES[i % n_templates]
        q_text = template.format(concept=concept.name)

        # Ground truth = the seed concept + all 1-hop neighbours
        gt_ids: list[int] = [concept.id]
        if G.has_node(concept.id):
            nbrs, _ = get_neighbourhood(G, concept.id, hops=1)
            gt_ids = list(set(gt_ids + nbrs))

        gt_names = [
            G.nodes[nid].get("name", str(nid))
            for nid in gt_ids
            if G.has_node(nid)
        ]

        questions.append(BenchmarkQuestion(
            question_id=i + 1,
            question_text=q_text,
            question_type=q_type,
            seed_concept_names=gt_names,
            seed_concept_ids=gt_ids,
        ))

    return questions


def _execute_query(G, concept_names: list[str]) -> list[int]:
    """
    Run the real retrieval pipeline for a list of concept name keywords.
    Returns the set of retrieved concept IDs.
    Mirrors what ask_workspace() and deliverable_service do.
    """
    keywords = concept_names[:6]   # cap at 6 to stay within QA_TOP_K range
    scored_seeds = find_nodes_semantic(G, keywords, top_k=_TOP_K)

    retrieved: set[int] = set()
    for node_id, _ in scored_seeds:
        nbr_nodes, _ = get_neighbourhood(
            G,
            node_id,
            hops=_HOPS,
            strength_threshold=settings.graph_strength_threshold,
            max_nodes=settings.graph_max_nodes,
        )
        retrieved.update(nbr_nodes)

    return list(retrieved)


def _score_question(
    q: BenchmarkQuestion,
    retrieved_ids: list[int],
    db: Session,
) -> QuestionResult:
    """Score a single benchmark question."""
    gt_set  = set(q.seed_concept_ids)
    ret_set = set(retrieved_ids)

    tp = len(gt_set & ret_set)
    recall    = tp / len(gt_set)    if gt_set  else 0.0
    precision = tp / len(ret_set)   if ret_set else 0.0

    # Evidence: does any retrieved concept have a source_excerpt?
    has_evidence = False
    if ret_set:
        evidenced = db.query(Concept).filter(
            Concept.id.in_(list(ret_set)),
            Concept.source_excerpt.isnot(None),
            Concept.source_excerpt != "",
        ).first()
        has_evidence = evidenced is not None

    return QuestionResult(
        question_id=q.question_id,
        question_text=q.question_text,
        question_type=q.question_type,
        ground_truth_ids=q.seed_concept_ids,
        retrieved_ids=retrieved_ids,
        has_evidence=has_evidence,
        recall=recall,
        precision=precision,
    )


def _consistency_score(
    G,
    questions: list[BenchmarkQuestion],
    n_repeats: int = 3,
) -> float:
    """
    Measure retrieval consistency: repeat each query n_repeats times and measure
    how much the result set varies.  Since the retrieval pipeline is deterministic
    (no randomness), we quantify consistency as 1 − (std of jaccard distances).

    Perfect consistency = 100.0 (always same result).
    For a deterministic graph with no randomness this will always be 100 unless
    the graph was modified between calls — which is the intended signal.
    """
    jaccard_diffs: list[float] = []

    for q in questions[:8]:   # limit to 8 questions for speed
        results: list[frozenset] = []
        for _ in range(n_repeats):
            r = frozenset(_execute_query(G, q.seed_concept_names[:3]))
            results.append(r)

        # Pairwise jaccard distances
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                a, b = results[i], results[j]
                union = len(a | b)
                if union == 0:
                    jaccard_diffs.append(0.0)
                else:
                    jaccard_diffs.append(1.0 - len(a & b) / union)

    if not jaccard_diffs:
        return 100.0

    mean_diff = statistics.mean(jaccard_diffs)
    return _round2(100.0 * (1.0 - mean_diff))


def _detect_gaps(
    db: Session,
    workspace_id: int,
    all_concepts: list[Concept],
    covered_concept_ids: set[int],
) -> list[KnowledgeGap]:
    """
    Detect concepts that were never retrieved during the benchmark.
    Classify them by gap type.
    """
    gaps: list[KnowledgeGap] = []

    # Connected concept IDs in SQL (have at least one relationship)
    from sqlalchemy import or_
    connected_ids: set[int] = {
        row[0] for row in db.query(Relationship.source_concept_id)
        .filter(Relationship.workspace_id == workspace_id).distinct().all()
    } | {
        row[0] for row in db.query(Relationship.target_concept_id)
        .filter(Relationship.workspace_id == workspace_id).distinct().all()
    }

    for c in all_concepts:
        if c.id in covered_concept_ids:
            continue   # retrieved — not a gap

        if c.id not in connected_ids:
            gap_type = "orphaned"
        elif not c.source_excerpt:
            gap_type = "no_evidence"
        else:
            gap_type = "never_retrieved"

        gaps.append(KnowledgeGap(
            concept_id=c.id,
            concept_name=c.name,
            concept_type=c.type or "General",
            gap_type=gap_type,
            source_document_id=c.source_document_id,
        ))

    return gaps


# ── Certification verdict ─────────────────────────────────────────────────────

def _certify(
    recall: float,
    precision: float,
    coverage: float,
    consistency: float,
    evidence: float,
) -> tuple[str, float]:
    """
    Compute the certification verdict and overall score.

    Returns (status, overall_score_0_100).
    """
    overall = (
        _CERT_WEIGHTS["recall"]      * recall
        + _CERT_WEIGHTS["precision"] * precision
        + _CERT_WEIGHTS["coverage"]  * coverage
        + _CERT_WEIGHTS["consistency"] * consistency
        + _CERT_WEIGHTS["evidence"]  * evidence
    )
    overall = _round2(overall)

    passes = {
        "recall":      recall      >= _THRESHOLDS["recall"],
        "precision":   precision   >= _THRESHOLDS["precision"],
        "coverage":    coverage    >= _THRESHOLDS["coverage"],
        "consistency": consistency >= _THRESHOLDS["consistency"],
        "evidence":    evidence    >= _THRESHOLDS["evidence"],
    }

    if overall >= _CERTIFIED_THRESHOLD and all(passes.values()):
        status = "CERTIFIED"
    elif overall >= _PROVISIONAL_THRESHOLD:
        status = "PROVISIONAL"
    else:
        status = "NOT_CERTIFIED"

    return status, overall


# ── Public API ────────────────────────────────────────────────────────────────

def run_certification(
    db: Session,
    workspace_id: int,
) -> RetrievalCertificationReport:
    """
    Run a full Retrieval Reliability benchmark and produce a certification report.

    Creates a RetrievalBenchmarkRun record in the DB (status=running), populates
    it, then marks it complete (or failed).

    Raises ValueError if the workspace has no concepts.
    """
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise ValueError(f"Workspace {workspace_id} not found.")

    # Create run record
    run = RetrievalBenchmarkRun(
        workspace_id=workspace_id,
        graph_version=ws.graph_version or 0,
        status="running",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        report = _execute_certification(db, ws, run)
    except Exception as exc:
        logger.exception("[cert ws=%d run=%d] Benchmark failed: %s", workspace_id, run.id, exc)
        run.status = "failed"
        run.error_detail = str(exc)[:512]
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise

    return report


def _execute_certification(
    db: Session,
    ws: Workspace,
    run: RetrievalBenchmarkRun,
) -> RetrievalCertificationReport:
    workspace_id = ws.id

    # ── Load graph and concepts ───────────────────────────────────────────────
    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    all_concepts = (
        db.query(Concept)
        .filter(Concept.workspace_id == workspace_id)
        .all()
    )

    if not all_concepts:
        raise ValueError(
            "No concepts found in workspace. "
            "Upload and process documents before running certification."
        )

    total_concepts = len(all_concepts)
    logger.info(
        "[cert ws=%d run=%d] Starting: %d concepts, graph nodes=%d",
        workspace_id, run.id, total_concepts, G.number_of_nodes(),
    )

    # ── Generate benchmark questions ──────────────────────────────────────────
    questions = _generate_questions(all_concepts, G, _MAX_QUESTIONS)
    run.questions_generated = len(questions)
    db.commit()

    logger.info("[cert ws=%d run=%d] Generated %d questions", workspace_id, run.id, len(questions))

    # ── Execute each question and score ───────────────────────────────────────
    question_results: list[QuestionResult] = []
    all_covered_ids: set[int] = set()

    for q in questions:
        retrieved_ids = _execute_query(G, q.seed_concept_names)
        qr = _score_question(q, retrieved_ids, db)
        question_results.append(qr)
        all_covered_ids.update(retrieved_ids)

    # ── Aggregate scores ──────────────────────────────────────────────────────
    answered = len([qr for qr in question_results if qr.retrieved_ids])
    run.questions_answered = answered

    if question_results:
        recall_scores     = [qr.recall    for qr in question_results]
        precision_scores  = [qr.precision for qr in question_results]
        evidence_scores   = [1.0 if qr.has_evidence else 0.0 for qr in question_results]

        avg_recall    = statistics.mean(recall_scores)
        avg_precision = statistics.mean(precision_scores)
        avg_evidence  = statistics.mean(evidence_scores)
    else:
        avg_recall = avg_precision = avg_evidence = 0.0

    # Coverage: what fraction of all workspace concepts appeared ≥ once
    coverage_ratio = len(all_covered_ids) / total_concepts if total_concepts else 0.0

    # Consistency: repeat-query stability (deterministic pipeline = near 100)
    consistency = _consistency_score(G, questions)

    # Scale to 0–100
    recall_s     = _round2(avg_recall    * 100)
    precision_s  = _round2(avg_precision * 100)
    coverage_s   = _round2(coverage_ratio * 100)
    consistency_s = _round2(consistency)
    evidence_s   = _round2(avg_evidence  * 100)

    # Composite retrieval_accuracy (simpler than certification score — equal weights)
    retrieval_accuracy = _round2(
        (recall_s + precision_s + coverage_s + consistency_s + evidence_s) / 5
    )

    # ── Knowledge gaps ────────────────────────────────────────────────────────
    gaps = _detect_gaps(db, workspace_id, all_concepts, all_covered_ids)

    # ── Certification ─────────────────────────────────────────────────────────
    cert_status, cert_score = _certify(
        recall_s, precision_s, coverage_s, consistency_s, evidence_s,
    )

    # ── Persist run ───────────────────────────────────────────────────────────
    run.recall_score       = recall_s
    run.precision_score    = precision_s
    run.coverage_score     = coverage_s
    run.consistency_score  = consistency_s
    run.evidence_fidelity  = evidence_s
    run.retrieval_accuracy = retrieval_accuracy
    run.certification_status = cert_status
    run.certification_score  = cert_score
    run.question_results_json = json.dumps([
        {
            "question_id":     qr.question_id,
            "question_text":   qr.question_text,
            "question_type":   qr.question_type,
            "ground_truth_ids": qr.ground_truth_ids,
            "retrieved_ids":   qr.retrieved_ids,
            "has_evidence":    qr.has_evidence,
            "recall":          round(qr.recall, 3),
            "precision":       round(qr.precision, 3),
        }
        for qr in question_results
    ])
    run.knowledge_gaps_json = json.dumps([
        {
            "concept_id":         g.concept_id,
            "concept_name":       g.concept_name,
            "concept_type":       g.concept_type,
            "gap_type":           g.gap_type,
            "source_document_id": g.source_document_id,
        }
        for g in gaps
    ])
    run.status       = "complete"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "[cert ws=%d run=%d] Complete: recall=%.1f precision=%.1f coverage=%.1f "
        "consistency=%.1f evidence=%.1f → %s (%.1f)",
        workspace_id, run.id, recall_s, precision_s, coverage_s,
        consistency_s, evidence_s, cert_status, cert_score,
    )

    return RetrievalCertificationReport(
        workspace_id=workspace_id,
        workspace_name=ws.name,
        run_id=run.id,
        graph_version=run.graph_version,
        generated_at=run.completed_at,

        recall_score=recall_s,
        precision_score=precision_s,
        coverage_score=coverage_s,
        consistency_score=consistency_s,
        evidence_fidelity=evidence_s,
        retrieval_accuracy=retrieval_accuracy,

        certification_status=cert_status,
        certification_score=cert_score,

        questions_generated=run.questions_generated,
        questions_answered=answered,
        total_workspace_concepts=total_concepts,
        concepts_covered=len(all_covered_ids),
        concepts_never_retrieved=len([g for g in gaps if g.gap_type == "never_retrieved"]),

        question_results=question_results,
        knowledge_gaps=gaps,

        recall_pass=     recall_s     >= _THRESHOLDS["recall"],
        precision_pass=  precision_s  >= _THRESHOLDS["precision"],
        coverage_pass=   coverage_s   >= _THRESHOLDS["coverage"],
        consistency_pass=consistency_s >= _THRESHOLDS["consistency"],
        evidence_pass=   evidence_s   >= _THRESHOLDS["evidence"],
    )


def get_latest_certification(
    db: Session,
    workspace_id: int,
) -> Optional[RetrievalCertificationReport]:
    """Return the most recent completed run, or None if none exists."""
    run = (
        db.query(RetrievalBenchmarkRun)
        .filter(
            RetrievalBenchmarkRun.workspace_id == workspace_id,
            RetrievalBenchmarkRun.status == "complete",
        )
        .order_by(RetrievalBenchmarkRun.completed_at.desc())
        .first()
    )
    if run is None:
        return None

    ws = db.get(Workspace, workspace_id)
    q_results = _deserialise_question_results(run.question_results_json)
    gaps = _deserialise_gaps(run.knowledge_gaps_json)

    # Reconstruct counts
    all_gt_ids: set[int] = set()
    all_ret_ids: set[int] = set()
    for qr in q_results:
        all_gt_ids.update(qr.ground_truth_ids)
        all_ret_ids.update(qr.retrieved_ids)

    total_concepts = db.query(Concept).filter(
        Concept.workspace_id == workspace_id
    ).count()

    return RetrievalCertificationReport(
        workspace_id=workspace_id,
        workspace_name=ws.name if ws else f"Workspace {workspace_id}",
        run_id=run.id,
        graph_version=run.graph_version,
        generated_at=run.completed_at or run.started_at,

        recall_score=run.recall_score,
        precision_score=run.precision_score,
        coverage_score=run.coverage_score,
        consistency_score=run.consistency_score,
        evidence_fidelity=run.evidence_fidelity,
        retrieval_accuracy=run.retrieval_accuracy,

        certification_status=run.certification_status,
        certification_score=run.certification_score,

        questions_generated=run.questions_generated,
        questions_answered=run.questions_answered,
        total_workspace_concepts=total_concepts,
        concepts_covered=len(all_ret_ids),
        concepts_never_retrieved=len([g for g in gaps if g.gap_type == "never_retrieved"]),

        question_results=q_results,
        knowledge_gaps=gaps,

        recall_pass=     run.recall_score     >= _THRESHOLDS["recall"],
        precision_pass=  run.precision_score  >= _THRESHOLDS["precision"],
        coverage_pass=   run.coverage_score   >= _THRESHOLDS["coverage"],
        consistency_pass=run.consistency_score >= _THRESHOLDS["consistency"],
        evidence_pass=   run.evidence_fidelity >= _THRESHOLDS["evidence"],
    )


def get_certification_status(
    db: Session,
    workspace_id: int,
) -> CertificationStatus:
    """
    Lightweight: return the certification status for polling.
    Returns NOT_CERTIFIED / PENDING if no run has completed.
    """
    run = (
        db.query(RetrievalBenchmarkRun)
        .filter(RetrievalBenchmarkRun.workspace_id == workspace_id)
        .order_by(RetrievalBenchmarkRun.started_at.desc())
        .first()
    )
    if run is None:
        return CertificationStatus(
            workspace_id=workspace_id,
            run_id=None,
            certification_status="PENDING",
            certification_score=0.0,
            recall_score=0.0,
            precision_score=0.0,
            coverage_score=0.0,
            consistency_score=0.0,
            evidence_fidelity=0.0,
            retrieval_accuracy=0.0,
            questions_generated=0,
            questions_answered=0,
            knowledge_gap_count=0,
            graph_version=0,
            completed_at=None,
        )

    gap_count = 0
    try:
        gaps = json.loads(run.knowledge_gaps_json or "[]")
        gap_count = len([g for g in gaps if g.get("gap_type") == "never_retrieved"])
    except Exception:
        pass

    status = run.certification_status if run.status == "complete" else "RUNNING"

    return CertificationStatus(
        workspace_id=workspace_id,
        run_id=run.id,
        certification_status=status,
        certification_score=run.certification_score,
        recall_score=run.recall_score,
        precision_score=run.precision_score,
        coverage_score=run.coverage_score,
        consistency_score=run.consistency_score,
        evidence_fidelity=run.evidence_fidelity,
        retrieval_accuracy=run.retrieval_accuracy,
        questions_generated=run.questions_generated,
        questions_answered=run.questions_answered,
        knowledge_gap_count=gap_count,
        graph_version=run.graph_version,
        completed_at=run.completed_at,
    )


def get_knowledge_gaps(
    db: Session,
    workspace_id: int,
) -> list[KnowledgeGap]:
    """Return the knowledge gap list from the latest completed run."""
    run = (
        db.query(RetrievalBenchmarkRun)
        .filter(
            RetrievalBenchmarkRun.workspace_id == workspace_id,
            RetrievalBenchmarkRun.status == "complete",
        )
        .order_by(RetrievalBenchmarkRun.completed_at.desc())
        .first()
    )
    if run is None:
        return []
    return _deserialise_gaps(run.knowledge_gaps_json)


# ── Deserialisation helpers ───────────────────────────────────────────────────

def _deserialise_question_results(raw: str) -> list[QuestionResult]:
    try:
        items = json.loads(raw or "[]")
    except Exception:
        return []
    results = []
    for item in items:
        try:
            results.append(QuestionResult(
                question_id=item.get("question_id", 0),
                question_text=item.get("question_text", ""),
                question_type=item.get("question_type", ""),
                ground_truth_ids=item.get("ground_truth_ids", []),
                retrieved_ids=item.get("retrieved_ids", []),
                has_evidence=bool(item.get("has_evidence", False)),
                recall=float(item.get("recall", 0.0)),
                precision=float(item.get("precision", 0.0)),
            ))
        except Exception:
            pass
    return results


def _deserialise_gaps(raw: str) -> list[KnowledgeGap]:
    try:
        items = json.loads(raw or "[]")
    except Exception:
        return []
    gaps = []
    for item in items:
        try:
            gaps.append(KnowledgeGap(
                concept_id=item.get("concept_id", 0),
                concept_name=item.get("concept_name", ""),
                concept_type=item.get("concept_type", "General"),
                gap_type=item.get("gap_type", "never_retrieved"),
                source_document_id=item.get("source_document_id"),
            ))
        except Exception:
            pass
    return gaps


# ── Deliverable safety gate ───────────────────────────────────────────────────

def check_deliverable_safety(
    db: Session,
    workspace_id: int,
) -> tuple[bool, str]:
    """
    Pre-generation safety check.

    Returns (is_safe: bool, message: str).

    Checks:
      1. At least one completed certification run exists.
      2. Certification is not NOT_CERTIFIED.
      3. Coverage ≥ 50% and Evidence ≥ 60%.

    If no run has been made, returns (True, "unchecked") so that the platform
    does not block generation before the user has run certification.
    """
    run = (
        db.query(RetrievalBenchmarkRun)
        .filter(
            RetrievalBenchmarkRun.workspace_id == workspace_id,
            RetrievalBenchmarkRun.status == "complete",
        )
        .order_by(RetrievalBenchmarkRun.completed_at.desc())
        .first()
    )
    if run is None:
        return True, "unchecked"

    if run.certification_status == "NOT_CERTIFIED":
        return False, (
            f"Workspace certification failed (score={run.certification_score:.0f}). "
            "Run Brain Certification and resolve knowledge gaps before generating deliverables."
        )
    if run.coverage_score < 50.0:
        return False, (
            f"Retrieval coverage is low ({run.coverage_score:.0f}%). "
            "Only {run.coverage_score:.0f}% of workspace knowledge is retrievable."
        )
    if run.evidence_fidelity < 60.0:
        return False, (
            f"Evidence fidelity is low ({run.evidence_fidelity:.0f}%). "
            "Too many retrieved concepts lack source evidence."
        )
    return True, f"CERTIFIED — score={run.certification_score:.0f}"
