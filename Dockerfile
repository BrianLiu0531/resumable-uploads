# 1) Build the React client
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# 2) Django app serving the API, the tus endpoint and the built client
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./
COPY --from=frontend /app/backend/frontend_dist ./frontend_dist
RUN SECRET_KEY=build-only python manage.py collectstatic --noinput
RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
# gthread workers: a slow client streaming a chunk occupies a thread, not a whole worker
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 8 --worker-class gthread --timeout 120 --access-logfile -"]
