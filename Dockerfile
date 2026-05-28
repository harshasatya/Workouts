FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uvicorn

COPY amazfit_mcp/ ./amazfit_mcp/

ENV MCP_TRANSPORT=http
ENV PORT=8000

EXPOSE 8000

CMD ["python", "-m", "amazfit_mcp.server"]
