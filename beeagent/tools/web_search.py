import httpx
from .base import BaseTool, ToolResult

class WebSearchTool(BaseTool):
    name = "web_search"
    description = "Search the internet"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    }
    
    def execute(self, query: str) -> ToolResult:
        try:
            resp = httpx.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            from html.parser import HTMLParser
            results = []
            class ResultParser(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.in_result = False
                    self.current = ""
                def handle_starttag(self, tag, attrs):
                    if tag == "a" and any(v == "result__a" for _, v in attrs):
                        self.in_result = True
                        self.current = ""
                def handle_endtag(self, tag):
                    if tag == "a" and self.in_result:
                        self.in_result = False
                        if self.current.strip():
                            results.append(self.current.strip())
                def handle_data(self, data):
                    if self.in_result:
                        self.current += data
            parser = ResultParser()
            parser.feed(resp.text)
            if not results:
                return ToolResult(output="No results found", error=False)
            output = "\n".join(f"{i+1}. {r}" for i, r in enumerate(results[:10]))
            return ToolResult(output=output, error=False)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return True