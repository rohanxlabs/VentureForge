from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path
from typing import Iterator
from src.research.models import EvidenceRecord, ResearchPlan, ResearchQuestion, ResearchRun, SourceSnapshot

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,text TEXT NOT NULL,priority TEXT NOT NULL DEFAULT 'high',source_categories_json TEXT NOT NULL DEFAULT '[]',evidence_required TEXT NOT NULL DEFAULT '',decision_impact TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,question_id TEXT NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,url TEXT NOT NULL,title TEXT NOT NULL,publisher TEXT NOT NULL,published_at TEXT,retrieved_at TEXT NOT NULL,content TEXT NOT NULL,search_snippet TEXT NOT NULL,search_score REAL,source_categories_json TEXT NOT NULL DEFAULT '[]',content_origin TEXT NOT NULL DEFAULT 'provider_raw_content',content_type TEXT NOT NULL DEFAULT '',final_url TEXT NOT NULL DEFAULT '',fetch_error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_sources_run_url ON sources(run_id,url);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,question_id TEXT NOT NULL REFERENCES research_questions(id) ON DELETE CASCADE,source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,claim TEXT NOT NULL,passage TEXT NOT NULL,relation TEXT NOT NULL,verification_status TEXT NOT NULL,verification_reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_plans(plan_id TEXT PRIMARY KEY,run_id TEXT NOT NULL UNIQUE REFERENCES research_runs(id) ON DELETE CASCADE,plan_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,approved_at TEXT);
CREATE TABLE IF NOT EXISTS solution_designs(design_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,status TEXT NOT NULL,created_at TEXT NOT NULL,design_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pitch_packages(package_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,design_id TEXT NOT NULL REFERENCES solution_designs(design_id),status TEXT NOT NULL,created_at TEXT NOT NULL,package_json TEXT NOT NULL);
"""

class ResearchStore:
    def __init__(self, path: str | Path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as c:
            c.executescript(SCHEMA)
            self._migrate_columns(c,"research_questions",{
                "priority":"TEXT NOT NULL DEFAULT 'high'",
                "source_categories_json":"TEXT NOT NULL DEFAULT '[]'",
                "evidence_required":"TEXT NOT NULL DEFAULT ''",
                "decision_impact":"TEXT NOT NULL DEFAULT ''",
                "status":"TEXT NOT NULL DEFAULT 'pending'",
            })
            self._migrate_columns(c,"sources",{
                "source_categories_json":"TEXT NOT NULL DEFAULT '[]'",
                "content_origin":"TEXT NOT NULL DEFAULT 'provider_raw_content'",
                "content_type":"TEXT NOT NULL DEFAULT ''",
                "final_url":"TEXT NOT NULL DEFAULT ''",
                "fetch_error":"TEXT NOT NULL DEFAULT ''",
            })

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        c=sqlite3.connect(self.path)
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _migrate_columns(c: sqlite3.Connection, table: str, columns: dict[str,str]) -> None:
        present={row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        for name,definition in columns.items():
            if name not in present:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @staticmethod
    def _insert_question(c: sqlite3.Connection, q: ResearchQuestion) -> None:
        c.execute(
            "INSERT INTO research_questions(id,run_id,text,priority,source_categories_json,evidence_required,decision_impact,status) VALUES(?,?,?,?,?,?,?,?)",
            (q.id,q.run_id,q.text,q.priority.value,json.dumps(q.source_categories),q.evidence_required,q.decision_impact,q.status),
        )

    @staticmethod
    def _question_from_row(row: sqlite3.Row) -> ResearchQuestion:
        data=dict(row)
        data["source_categories"]=json.loads(data.pop("source_categories_json","[]"))
        return ResearchQuestion.model_validate(data)

    def save_run(self, run: ResearchRun) -> None:
        with self.db() as c:
            c.execute("INSERT INTO research_runs VALUES(?,?,?)",(run.id,run.prompt,run.created_at.isoformat()))

    def save_question(self, question: ResearchQuestion) -> None:
        with self.db() as c:
            self._insert_question(c,question)

    def save_source(self, source: SourceSnapshot) -> SourceSnapshot:
        with self.db() as c:
            url_candidates=list(dict.fromkeys(value for value in (source.url,source.final_url) if value))
            placeholders=",".join("?" for _ in url_candidates)
            existing=c.execute(f"SELECT * FROM sources WHERE run_id=? AND (url IN ({placeholders}) OR final_url IN ({placeholders})) ORDER BY rowid LIMIT 1",
                (source.run_id,*url_candidates,*url_candidates)).fetchone()
            if existing is not None:
                categories=list(dict.fromkeys(json.loads(existing["source_categories_json"] or "[]")+source.source_categories))
                has_new_content=not existing["content"] and bool(source.content)
                c.execute("""UPDATE sources SET source_categories_json=?,
                    content=CASE WHEN content='' AND ? THEN ? ELSE content END,
                    search_snippet=CASE WHEN search_snippet='' THEN ? ELSE search_snippet END,
                    title=CASE WHEN title='' THEN ? ELSE title END,
                    publisher=CASE WHEN publisher='' THEN ? ELSE publisher END,
                    search_score=COALESCE(search_score,?),
                    content_origin=CASE WHEN content='' AND ? THEN ? ELSE content_origin END,
                    content_type=CASE WHEN content='' AND ? THEN ? ELSE content_type END,
                    final_url=CASE WHEN final_url='' THEN ? ELSE final_url END,
                    fetch_error=CASE WHEN fetch_error='' THEN ? ELSE fetch_error END
                    WHERE id=?""",
                    (json.dumps(categories),int(has_new_content),source.content,source.search_snippet,
                     source.title,source.publisher,source.search_score,int(has_new_content),source.content_origin,
                     int(has_new_content),source.content_type,source.final_url,source.fetch_error,existing["id"]))
                return source.model_copy(update={"id":existing["id"],
                    "source_categories":categories,
                    "content":source.content if has_new_content else existing["content"],
                    "content_origin":source.content_origin if has_new_content else existing["content_origin"],
                    "content_type":source.content_type if has_new_content else existing["content_type"],
                    "final_url":source.final_url or existing["final_url"]})
            c.execute(
                """INSERT INTO sources
                (id,run_id,question_id,url,title,publisher,published_at,retrieved_at,content,
                 search_snippet,search_score,source_categories_json,content_origin,content_type,final_url,fetch_error)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (source.id,source.run_id,source.question_id,source.url,source.title,source.publisher,
                 source.published_at,source.retrieved_at.isoformat(),source.content,source.search_snippet,
                 source.search_score,json.dumps(source.source_categories),source.content_origin,
                 source.content_type,source.final_url,source.fetch_error),
            )
            return source

    def save_evidence(self, evidence: EvidenceRecord) -> None:
        with self.db() as c:
            existing=c.execute("SELECT id FROM evidence WHERE question_id=? AND source_id=? AND claim=? AND passage=?",
                (evidence.question_id,evidence.source_id,evidence.claim,evidence.passage)).fetchone()
            if existing:
                c.execute("UPDATE evidence SET relation=?,verification_status=?,verification_reason=? WHERE id=?",
                    (evidence.relation.value,evidence.verification_status.value,evidence.verification_reason,existing["id"]))
                return
            c.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?)",
                (evidence.id,evidence.run_id,evidence.question_id,evidence.source_id,evidence.claim,
                 evidence.passage,evidence.relation.value,evidence.verification_status.value,
                 evidence.verification_reason,evidence.created_at.isoformat()),
            )

    def update_question_status(self, question_id: str, status: str) -> None:
        if status not in {"pending", "in_progress", "covered", "blocked"}:
            raise ValueError(f"Invalid question status: {status}")
        with self.db() as c:
            cursor=c.execute("UPDATE research_questions SET status=? WHERE id=?",(status,question_id))
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown research question: {question_id}")

    def save_solution_design(self, design) -> None:
        with self.db() as c:
            c.execute("INSERT INTO solution_designs VALUES(?,?,?,?,?)",
                (design.id,design.run_id,design.status,design.created_at.isoformat(),design.model_dump_json()))

    def get_solution_design(self, design_id: str):
        from src.research.solution import SolutionDesign
        with self.db() as c:
            row=c.execute("SELECT design_json FROM solution_designs WHERE design_id=?",(design_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown solution design: {design_id}")
        return SolutionDesign.model_validate_json(row["design_json"])

    def get_latest_solution_design(self, run_id: str):
        from src.research.solution import SolutionDesign
        with self.db() as c:
            row=c.execute("SELECT design_json FROM solution_designs WHERE run_id=? ORDER BY created_at DESC LIMIT 1",(run_id,)).fetchone()
        if row is None:
            raise KeyError(f"No solution design exists for research run: {run_id}")
        return SolutionDesign.model_validate_json(row["design_json"])

    def save_pitch_package(self, package) -> None:
        with self.db() as c:
            c.execute("INSERT INTO pitch_packages VALUES(?,?,?,?,?,?)",
                (package.id,package.run_id,package.design_id,package.status,package.created_at.isoformat(),package.model_dump_json()))

    def get_pitch_package(self, package_id: str):
        from src.research.pitch import PitchPackage
        with self.db() as c:
            row=c.execute("SELECT package_json FROM pitch_packages WHERE package_id=?",(package_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown pitch package: {package_id}")
        return PitchPackage.model_validate_json(row["package_json"])

    def save_plan(self, plan: ResearchPlan) -> None:
        with self.db() as c:
            c.execute("INSERT INTO research_runs(id,prompt,created_at) VALUES(?,?,?)",
                (plan.run_id,plan.brief.problem_statement,plan.created_at.isoformat()))
            c.execute("INSERT INTO research_plans VALUES(?,?,?,?,?,?)",
                (plan.id,plan.run_id,plan.model_dump_json(),plan.status,plan.created_at.isoformat(),
                 plan.approved_at.isoformat() if plan.approved_at else None))
            for question in plan.questions:
                self._insert_question(c,question)

    def update_plan(self, plan: ResearchPlan) -> None:
        with self.db() as c:
            row=c.execute("SELECT status FROM research_plans WHERE plan_id=?",(plan.id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown research plan: {plan.id}")
            if row["status"]!="draft" and plan.status!=row["status"]:
                raise ValueError("Only a draft research plan can change status.")
            c.execute("UPDATE research_plans SET plan_json=?,status=?,approved_at=? WHERE plan_id=?",
                (plan.model_dump_json(),plan.status,plan.approved_at.isoformat() if plan.approved_at else None,plan.id))
            for q in plan.questions:
                c.execute("""UPDATE research_questions SET text=?,priority=?,source_categories_json=?,
                    evidence_required=?,decision_impact=?,status=? WHERE id=? AND run_id=?""",
                    (q.text,q.priority.value,json.dumps(q.source_categories),q.evidence_required,
                     q.decision_impact,q.status,q.id,plan.run_id))

    def get_plan(self, plan_id_or_run_id: str) -> ResearchPlan:
        with self.db() as c:
            row=c.execute("SELECT plan_json FROM research_plans WHERE plan_id=? OR run_id=?",
                (plan_id_or_run_id,plan_id_or_run_id)).fetchone()
        if row is None:
            raise KeyError(f"Unknown research plan: {plan_id_or_run_id}")
        return ResearchPlan.model_validate_json(row["plan_json"])

    def get_run_records(self, run_id: str):
        with self.db() as c:
            run=c.execute("SELECT * FROM research_runs WHERE id=?",(run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Unknown research run: {run_id}")
            questions=c.execute("SELECT * FROM research_questions WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            source_rows=c.execute("SELECT * FROM sources WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            evidence=c.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
        sources=[]
        for row in source_rows:
            data=dict(row)
            data["source_categories"]=json.loads(data.pop("source_categories_json","[]"))
            sources.append(SourceSnapshot.model_validate(data))
        return (
            ResearchRun.model_validate(dict(run)),
            [self._question_from_row(row) for row in questions],
            sources,
            [EvidenceRecord.model_validate(dict(row)) for row in evidence],
        )
