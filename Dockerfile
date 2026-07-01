FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8848
ENV AMY_VAULT=/vault
CMD ["uvicorn", "amy.app:app", "--host", "0.0.0.0", "--port", "8848"]
