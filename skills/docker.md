# Skill: Docker & Container Deployment

Use these rules when working on Docker containerization, docker-compose, or cloud container deployment tasks.

## Required Tool Chain
- `docker build` - always pass `--no-cache` on first build attempt
- `docker-compose up -d` for multi-service stacks
- Use `run_command` with `docker logs <container> --tail 50` to monitor startup

## Docker Best Practices
- Multi-stage builds: separate `builder` and `runtime` stages to minimize image size
- Always pin base image versions: `FROM python:3.12-slim` not `FROM python:latest`
- Use `.dockerignore` to exclude `node_modules`, `.git`, `venv`, `__pycache__`
- Expose only required ports; map 0.0.0.0 binding explicitly

## Compose Patterns
- Health checks: `healthcheck: test: ["CMD", "curl", "-f", "http://localhost:PORT/health"]`
- Named volumes for persistent data; never use bind mounts in production containers
- Use `restart: unless-stopped` for long-running services

## Debugging
- `docker exec -it <container> /bin/sh` for live inspection
- `docker system prune -f` to clear stale images/volumes
