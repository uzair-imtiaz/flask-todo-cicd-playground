# flask-todo

A small Flask + Postgres todo service used as the running example throughout this course. Subsequent modules containerize it, deploy it to ECS, move it to EKS, instrument it, and put it behind ArgoCD.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/healthz` | Liveness probe. Always 200 if the process is up. Does not touch the DB. |
| `GET` | `/readyz` | Readiness probe. 200 if DB is reachable, 503 otherwise. |
| `GET` | `/todos` | List todos. Returns a JSON array. |
| `POST` | `/todos` | Create a todo. Body: `{"title": "..."}`. Returns 201 + the todo. |

## Configuration (env vars)

| Var | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | yes | - | Postgres connection string, e.g. `postgresql://user:pass@host:5432/db` |
| `LOG_LEVEL` | no | `INFO` | Standard Python log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PORT` | no | `8000` | TCP port to listen on |
| `DB_POOL_MIN` | no | `1` | Minimum connections held in the pool |
| `DB_POOL_MAX` | no | `10` | Maximum connections the pool may open |

## Logs

Structured JSON to stdout, one event per line. Each line has `ts`, `level`, `logger`, `msg`.

## Schema

The app creates the `todos` table on first request if it does not exist:

```sql
CREATE TABLE todos (
  id SERIAL PRIMARY KEY,
  title TEXT NOT NULL,
  done BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Schema migrations are out of scope for this course. The table is recreated as needed.

## Running locally

```bash
# Start Postgres in Docker
docker run -d --name todo-db \
  -e POSTGRES_PASSWORD=postgres \
  -p 5432:5432 \
  postgres:16

# Install dependencies (in a venv)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run
export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
python app.py

# In another terminal, exercise the API
curl http://localhost:8000/healthz
curl http://localhost:8000/readyz
curl -X POST http://localhost:8000/todos \
     -H 'Content-Type: application/json' \
     -d '{"title": "first todo"}'
curl http://localhost:8000/todos
```

## How the course evolves this app

| Module | What changes |
|---|---|
| 2 (Containers) | Containerize, push to a local registry |
| 3 (CI/CD) | Build & push image to ECR via GitHub Actions |
| 4 (AWS) | Deploy to ECS Fargate behind an ALB |
| 5 (EKS) | Re-deploy as a Kubernetes Deployment + Service + Ingress |
| 6 (Terraform) | Add a small Terraform change (e.g., new S3 bucket the app uses) |
| 7 (Observability) | Add CloudWatch alarms + log queries |
| 8 (GitOps) | Convert deployment to ArgoCD-managed |
