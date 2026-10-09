# Student portal image, built on the scoring engine by portal_ops.deploy_portal.
#
# Stage 1 builds the frontend (portal/frontend: React + Vite + Tailwind, the same stack as
# webui/frontend) with `npm ci`, so every dependency — noVNC included, pinned to its v1.6.0
# release tarball — is checked against package-lock.json's integrity hashes.
FROM node:22.21.0-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt
COPY auth.py console.py app.py ./
COPY --from=web /web/dist ./frontend/dist

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
