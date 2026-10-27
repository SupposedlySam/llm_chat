# The llm_chat hub: one stdlib Python file, nothing to install.
FROM python:3.12-slim
WORKDIR /app
COPY llm_chat_hub.py .
ENV HUB_DB=/data/hub.db HUB_AUTH=access PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "llm_chat_hub.py", "--port", "8080"]
