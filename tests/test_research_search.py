import ipaddress
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import requests
from src.research.budget import BudgetExceeded,SearchBudget
from src.research.collection import collect_plan_sources
from src.research.connectors import TavilyConnector,request_json
from src.research.models import ResearchQuestion,SourceSnapshot
from src.research.safe_fetch import SafeFetcher,UnsafeURLError,html_to_text
from src.research.search import SearchOrchestrator
from src.research.storage import ResearchStore

class MemoryCache:
    def __init__(self): self.items={}
    def get(self,key,default=None): return self.items.get(key,default)
    def set(self,key,value,expire=None): self.items[key]=value

class Response:
    def __init__(self,status,payload):
        self.status_code=status; self.payload=payload; self.headers={}
    def raise_for_status(self):
        if self.status_code>=400: raise requests.HTTPError(f"HTTP {self.status_code}")
    def json(self): return self.payload

class RequestAndCacheTests(unittest.TestCase):
    def test_transient_server_error_retries_then_succeeds(self):
        class Session:
            def __init__(self): self.responses=[Response(503,{}),Response(200,{"ok":True})]; self.calls=0
            def post(self,*args,**kwargs): self.calls+=1; return self.responses.pop(0)
        session=Session(); delays=[]
        result=request_json(session,"post","https://example.test",timeout=1,retries=2,sleep_fn=delays.append)
        self.assertTrue(result["ok"]); self.assertEqual(session.calls,2); self.assertEqual(delays,[0.5])

    def test_permanent_http_error_is_not_retried(self):
        class Session:
            calls=0
            def post(self,*args,**kwargs): self.calls+=1; return Response(401,{})
        session=Session()
        with self.assertRaises(requests.HTTPError):
            request_json(session,"post","https://example.test",timeout=1,retries=3,sleep_fn=lambda _:None)
        self.assertEqual(session.calls,1)

    def test_tavily_cache_reuses_payload_but_rebinds_run_ids(self):
        class Session:
            calls=0
            def post(self,*args,**kwargs):
                self.calls+=1
                return Response(200,{"results":[{"url":"https://example.org/a","title":"A","raw_content":"Captured source","content":"snippet"}]})
        session=Session()
        connector=TavilyConnector("test-key",session=session,cache=MemoryCache(),sleep_fn=lambda _:None)
        first=connector.search(run_id="run-1",question_id="q-1",query="query",category="official")
        second=connector.search(run_id="run-2",question_id="q-2",query="query",category="official")
        self.assertEqual(session.calls,1)
        self.assertEqual(second[0].run_id,"run-2")
        self.assertNotEqual(first[0].id,second[0].id)
        self.assertEqual(second[0].source_categories,["official"])
        self.assertEqual(second[0].content_origin,"provider_raw_content")

class SafeFetcherTests(unittest.TestCase):
    def test_blocks_local_ip_before_opening_socket(self):
        called=[]
        fetcher=SafeFetcher(connection_factory=lambda *a,**k:called.append(True))
        with self.assertRaises(UnsafeURLError): fetcher.fetch("http://127.0.0.1/private")
        self.assertEqual(called,[])

    def test_blocks_dns_results_containing_private_addresses(self):
        def resolver(host,port,type):
            return [(None,None,None,None,("8.8.8.8",port)),(None,None,None,None,("10.0.0.1",port))]
        fetcher=SafeFetcher(resolver=resolver)
        with self.assertRaises(UnsafeURLError): fetcher._target("https://example.org/report")

    def test_rejects_unsupported_scheme_and_nonstandard_port(self):
        fetcher=SafeFetcher()
        with self.assertRaises(UnsafeURLError): fetcher._target("file:///etc/passwd")
        with self.assertRaises(UnsafeURLError): fetcher._target("http://example.org:8080/")

    def test_html_text_extraction_drops_script_and_markup(self):
        text=html_to_text("<html><script>secret()</script><h1>Title</h1><p>Visible text</p></html>")
        self.assertIn("Title",text); self.assertIn("Visible text",text); self.assertNotIn("secret",text)

class BudgetAndOrchestrationTests(unittest.TestCase):
    def test_invalid_budgets_and_worker_counts_are_rejected(self):
        with self.assertRaises(ValueError): SearchBudget(max_pages=-1)
        with self.assertRaises(ValueError): SearchBudget(max_seconds=float("inf"))
        with self.assertRaises(ValueError): SearchOrchestrator(None,None,max_workers=0)

    def test_repeated_collection_updates_existing_source_instead_of_duplicating(self):
        from src.research.models import ResearchRun
        with tempfile.TemporaryDirectory() as temp:
            store=ResearchStore(Path(temp)/"research.sqlite")
            run=ResearchRun(prompt="prompt"); store.save_run(run)
            q=ResearchQuestion(run_id=run.id,text="question",source_categories=["official"]); store.save_question(q)
            first=SourceSnapshot(run_id=run.id,question_id=q.id,url="https://example.org/a",title="A",source_categories=["official"])
            second=SourceSnapshot(run_id=run.id,question_id=q.id,url="https://example.org/a",title="A",content="Captured evidence",source_categories=["community"],content_origin="safe_fetch")
            store.save_source(first); store.save_source(second)
            _,_,sources,_=store.get_run_records(run.id)
            self.assertEqual(len(sources),1)
            self.assertEqual(sources[0].content,"Captured evidence")
            self.assertCountEqual(sources[0].source_categories,["official","community"])

    def test_budgets_stop_excess_search_and_fetch(self):
        budget=SearchBudget(max_search_calls=1,max_tavily_credits=1,max_pages=0)
        budget.reserve_search("tavily",1)
        with self.assertRaises(BudgetExceeded): budget.reserve_search("tavily",1)
        with self.assertRaises(BudgetExceeded): budget.reserve_page()

    def test_parallel_connectors_merge_duplicate_urls_and_categories(self):
        class Fake:
            def __init__(self,name,cost): self.name=name; self.cost_per_search=cost
            def search(self,*,run_id,question_id,query,category,max_results):
                return [SourceSnapshot(run_id=run_id,question_id=question_id,url="https://example.org/source?utm_source=x",title="Source",content="Captured text",source_categories=[category],content_origin="api_content")]
        q=ResearchQuestion(run_id="r",text="Question?",source_categories=["official","community"])
        result=SearchOrchestrator(Fake("tavily",1),Fake("hackernews",0),fetcher=object(),max_workers=3).collect(
            run_id="r",prompt="Prompt",questions=[q],budget=SearchBudget(max_search_calls=3,max_tavily_credits=2))
        self.assertEqual(result.search_calls,3)
        self.assertEqual(result.tavily_credits,2)
        self.assertEqual(len(result.sources),1)
        self.assertCountEqual(result.sources[0].source_categories,["official","community"])

    def test_page_budget_exhaustion_keeps_search_result_unverified(self):
        class Tavily:
            name="tavily"; cost_per_search=1
            def search(self,*,run_id,question_id,query,category,max_results):
                return [SourceSnapshot(run_id=run_id,question_id=question_id,url="https://example.org/a",search_snippet="snippet",source_categories=[category],content_origin="search_result_only")]
        class HN:
            name="hackernews"; cost_per_search=0
            def search(self,**kwargs): return []
        class Fetcher:
            def fetch(self,url): raise AssertionError("Page budget should prevent fetch")
        q=ResearchQuestion(run_id="r",text="Question?",source_categories=["official"])
        result=SearchOrchestrator(Tavily(),HN(),fetcher=Fetcher()).collect(run_id="r",prompt="",questions=[q],budget=SearchBudget(max_pages=0))
        self.assertEqual(result.sources[0].content_origin,"search_result_only")
        self.assertTrue(result.failures)

    def test_collection_rejects_unapproved_plan(self):
        with self.assertRaisesRegex(ValueError,"approved plan"):
            collect_plan_sources(SimpleNamespace(status="draft"),None)

    def test_source_migration_adds_collection_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"old.sqlite"; c=__import__("sqlite3").connect(path)
            c.executescript("CREATE TABLE research_runs(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,created_at TEXT NOT NULL); CREATE TABLE research_questions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,text TEXT NOT NULL); CREATE TABLE sources(id TEXT PRIMARY KEY,run_id TEXT,question_id TEXT,url TEXT,title TEXT,publisher TEXT,published_at TEXT,retrieved_at TEXT,content TEXT,search_snippet TEXT,search_score REAL);")
            c.close(); store=ResearchStore(path)
            with store.db() as db: names={x["name"] for x in db.execute("PRAGMA table_info(sources)")}
            self.assertTrue({"source_categories_json","content_origin","content_type","final_url","fetch_error"}.issubset(names))

if __name__=="__main__": unittest.main()
