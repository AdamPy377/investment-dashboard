FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py worker.py backup.py csv_import.py reports.py journal.py ./
COPY static ./static
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001
EXPOSE 3000
CMD ["gunicorn", "-w", "1", "--threads", "4", "--bind", "0.0.0.0:3000", "--timeout", "90", "app:app"]
