from __future__ import annotations
from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
import ipaddress
import math
import socket
import ssl
from urllib.parse import urljoin,urlsplit

class UnsafeURLError(ValueError): pass
class FetchError(RuntimeError): pass

@dataclass(frozen=True)
class FetchedPage:
    final_url:str
    text:str
    content_type:str
    status_code:int

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag.lower() in {"script","style","noscript","svg"}: self.hidden+=1
        elif tag.lower() in {"p","div","br","li","tr","h1","h2","h3","section"}: self.parts.append("\n")
    def handle_endtag(self,tag):
        if tag.lower() in {"script","style","noscript","svg"} and self.hidden: self.hidden-=1
        elif tag.lower() in {"p","div","li","tr","h1","h2","h3","section"}: self.parts.append("\n")
    def handle_data(self,data):
        if not self.hidden and data.strip(): self.parts.append(data.strip())

def html_to_text(value:str)->str:
    parser=_TextExtractor(); parser.feed(value)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())

class SafeFetcher:
    """Fetch public HTTP(S) text pages with IP pinning, redirect checks, and byte limits."""
    def __init__(self,*,timeout_seconds:float=12,max_bytes:int=2_000_000,max_redirects:int=5,resolver=None,connection_factory=None,ssl_context=None):
        if not math.isfinite(timeout_seconds) or timeout_seconds<=0: raise ValueError("Fetch timeout must be finite and positive.")
        if max_bytes<1: raise ValueError("Fetch byte limit must be positive.")
        if max_redirects<0: raise ValueError("Redirect limit cannot be negative.")
        self.timeout=timeout_seconds; self.max_bytes=max_bytes; self.max_redirects=max_redirects
        self.resolver=resolver or socket.getaddrinfo
        self.connection_factory=connection_factory or socket.create_connection
        self.ssl_context=ssl_context or ssl.create_default_context()
    def _target(self,url:str):
        parsed=urlsplit(url); scheme=parsed.scheme.lower()
        if scheme not in {"http","https"}: raise UnsafeURLError("Only HTTP and HTTPS URLs are allowed.")
        if parsed.username or parsed.password: raise UnsafeURLError("URLs containing credentials are not allowed.")
        host=parsed.hostname
        if not host: raise UnsafeURLError("URL has no hostname.")
        try: port=parsed.port or (443 if scheme=="https" else 80)
        except ValueError as exc: raise UnsafeURLError("URL has an invalid port.") from exc
        if (scheme=="http" and port!=80) or (scheme=="https" and port!=443): raise UnsafeURLError("Only default HTTP and HTTPS ports are allowed.")
        if host.lower().endswith((".localhost",".local",".internal")) or host.lower()=="localhost": raise UnsafeURLError("Local and internal hostnames are blocked.")
        try: addresses=[ipaddress.ip_address(host)]
        except ValueError:
            try: answers=self.resolver(host,port,type=socket.SOCK_STREAM)
            except OSError as exc: raise FetchError(f"Could not resolve source host: {exc}") from exc
            addresses=[]
            for answer in answers:
                try: addresses.append(ipaddress.ip_address(answer[4][0].split("%")[0]))
                except ValueError: continue
        if not addresses: raise FetchError("Source host did not resolve to an IP address.")
        if any(not address.is_global for address in addresses): raise UnsafeURLError("Source host resolves to a non-public IP address.")
        return parsed,host,port,str(addresses[0])
    def fetch(self,url:str)->FetchedPage:
        current=url
        for redirect_index in range(self.max_redirects+1):
            parsed,host,port,ip=self._target(current)
            sock=self.connection_factory((ip,port),timeout=self.timeout); conn=None
            try:
                if parsed.scheme.lower()=="https": sock=self.ssl_context.wrap_socket(sock,server_hostname=host)
                conn=http.client.HTTPConnection(host,port,timeout=self.timeout); conn.sock=sock
                target=parsed.path or "/"
                if parsed.query: target+="?"+parsed.query
                host_header=f"[{host}]" if ":" in host else host
                if parsed.port: host_header+=f":{parsed.port}"
                conn.putrequest("GET",target,skip_accept_encoding=True)
                conn.putheader("Host",host_header); conn.putheader("User-Agent","VentureForgeResearch/1.0")
                conn.putheader("Accept","text/html, text/plain, application/xhtml+xml")
                conn.putheader("Connection","close"); conn.endheaders()
                response=conn.getresponse()
                if response.status in {301,302,303,307,308}:
                    location=response.getheader("Location")
                    if not location: raise FetchError("Redirect response omitted Location.")
                    if redirect_index>=self.max_redirects: raise FetchError("Too many redirects.")
                    current=urljoin(current,location); continue
                if response.status<200 or response.status>=300: raise FetchError(f"Source returned HTTP {response.status}.")
                content_type=response.getheader("Content-Type","").split(";",1)[0].strip().lower()
                if content_type not in {"text/html","application/xhtml+xml","text/plain"}: raise FetchError(f"Unsupported source content type: {content_type or 'unknown'}.")
                length=response.getheader("Content-Length")
                if length and int(length)>self.max_bytes: raise FetchError("Source exceeds the configured byte limit.")
                raw=response.read(self.max_bytes+1)
                if len(raw)>self.max_bytes: raise FetchError("Source exceeds the configured byte limit.")
                charset="utf-8"
                for part in response.getheader("Content-Type","").split(";")[1:]:
                    if part.strip().lower().startswith("charset="): charset=part.split("=",1)[1].strip(" \"'")
                decoded=raw.decode(charset,errors="replace")
                text=html_to_text(decoded) if content_type in {"text/html","application/xhtml+xml"} else decoded.strip()
                return FetchedPage(final_url=current,text=text,content_type=content_type,status_code=response.status)
            except (UnsafeURLError,FetchError): raise
            except (OSError,http.client.HTTPException,ssl.SSLError) as exc: raise FetchError(f"Could not fetch source: {exc}") from exc
            finally:
                if conn is not None: conn.close()
                else:
                    try: sock.close()
                    except OSError: pass
        raise FetchError("Redirect processing failed.")
