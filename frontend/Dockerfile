# syntax=docker/dockerfile:1.4
FROM node:20-alpine AS builder

WORKDIR /app

# Law 9: Layer Caching Defense
COPY package*.json ./
RUN npm ci

COPY . .

# Vite requires VITE_ prefixed environment variables at build time
# Use relative API path to allow Nginx reverse proxying to handle routing
ENV VITE_API_BASE_URL="/api/v1"
RUN npm run build


FROM nginx:1.25-alpine AS runner

# Law 8: Non-Root Supremacy
RUN addgroup -g 10001 appgroup && \
    adduser -u 10001 -G appgroup -s /bin/sh -D appuser

# Copy custom Nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built assets
COPY --from=builder /app/dist /usr/share/nginx/html

# Set permissions for Nginx non-root execution
RUN chown -R appuser:appgroup /usr/share/nginx/html && \
    chown -R appuser:appgroup /var/cache/nginx && \
    chown -R appuser:appgroup /var/log/nginx && \
    chown -R appuser:appgroup /etc/nginx/conf.d && \
    touch /var/run/nginx.pid && \
    chown -R appuser:appgroup /var/run/nginx.pid

USER appuser

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
