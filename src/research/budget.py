from __future__ import annotations
from dataclasses import dataclass,field
import math
import threading
import time

class BudgetExceeded(RuntimeError):
    pass

@dataclass
class SearchBudget:
    max_search_calls:int=8
    max_pages:int=20
    max_tavily_credits:int=8
    max_seconds:float=180
    started_at:float=field(default_factory=time.monotonic)
    search_calls:int=0
    pages_fetched:int=0
    tavily_credits:int=0
    _lock:threading.Lock=field(default_factory=threading.Lock,repr=False)

    def __post_init__(self)->None:
        if self.max_search_calls<0 or self.max_pages<0 or self.max_tavily_credits<0:
            raise ValueError("Search, page, and credit budgets cannot be negative.")
        if not math.isfinite(self.max_seconds) or self.max_seconds<=0:
            raise ValueError("The time budget must be a finite positive number.")

    def elapsed(self)->float:
        return max(0.0,time.monotonic()-self.started_at)

    def remaining_seconds(self)->float:
        return max(0.0,self.max_seconds-self.elapsed())

    def _check_time(self)->None:
        if self.remaining_seconds()<=0: raise BudgetExceeded("Research time budget exhausted.")

    def reserve_search(self,connector_name:str,cost:int)->None:
        with self._lock:
            self._check_time()
            if self.search_calls>=self.max_search_calls: raise BudgetExceeded("Search-call budget exhausted.")
            if connector_name=="tavily" and self.tavily_credits+cost>self.max_tavily_credits:
                raise BudgetExceeded("Tavily credit budget exhausted.")
            self.search_calls+=1
            if connector_name=="tavily": self.tavily_credits+=cost

    def reserve_page(self)->None:
        with self._lock:
            self._check_time()
            if self.pages_fetched>=self.max_pages: raise BudgetExceeded("Fetched-page budget exhausted.")
            self.pages_fetched+=1

    def snapshot(self)->dict:
        with self._lock:
            return {"search_calls":self.search_calls,"pages_fetched":self.pages_fetched,
                "tavily_credits":self.tavily_credits,"elapsed_seconds":round(self.elapsed(),2)}
