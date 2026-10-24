# The key proxy: a tiny service that holds the provider keys so the agent container never does.
FROM python:3.12-slim
RUN pip install --no-cache-dir "httpx==0.28.1" "aiohttp==3.11.18"
COPY proxy.py subscriptions.py claude.py clients.py /app/
ENV PYTHONPATH=/app
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3200/healthz', timeout=2).status == 200 else 1)"]
CMD ["python", "/app/proxy.py"]
