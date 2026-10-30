# Frontend image: static files behind nginx-unprivileged. No Node in the runtime image.
# Build:  docker build -t ekc-web apps/web   (context = apps/web)
# Base images are referenced by tag; CI should pass scanned digests.

FROM node:22.11-alpine3.20 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27.2-alpine AS runtime
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="ekc-web" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
# The base image's default site is replaced; headers live in an include so every location
# repeats them (see security_headers.conf).
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY nginx/security_headers.conf /etc/nginx/ekc_security_headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
