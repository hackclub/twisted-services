#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
if [[ ! -f "$ENV_FILE" ]]; then
	printf 'Missing %s. Copy env.sample to .env and configure it first.\n' "$ENV_FILE" >&2
	exit 1
fi

# Export simple KEY=VALUE dotenv entries without executing the file. This
# supports values containing spaces, such as APP_NAME=Twisted Services.
while IFS= read -r line || [[ -n "$line" ]]; do
	line="${line#"${line%%[![:space:]]*}"}"
	line="${line%"${line##*[![:space:]]}"}"
	[[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
	key="${line%%=*}"
	value="${line#*=}"
	key="${key#"${key%%[![:space:]]*}"}"
	key="${key%"${key##*[![:space:]]}"}"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	if [[ "$value" == '"'*'"' || "$value" == "'"*"'" ]]; then
		value="${value:1:${#value}-2}"
	fi
	[[ "$key" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || continue
	export "$key=$value"
done < "$ENV_FILE"

PID_FILE="${BOT_PID_FILE:-$ROOT_DIR/.twisted-services.pid}"
LOG_FILE="${BOT_LOG_FILE:-$ROOT_DIR/twisted-services.log}"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
	if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
		PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
	else
		PYTHON_BIN="python3"
	fi
fi

stop_bot() {
	if [[ -f "$PID_FILE" ]]; then
		local old_pid
		old_pid="$(<"$PID_FILE")"
		if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
			printf 'Stopping existing bot (pid %s)...\n' "$old_pid"
			kill "$old_pid"
			for _ in {1..20}; do
				kill -0 "$old_pid" 2>/dev/null || break
				read -r -t 0.25 _ </dev/null || true
			done
			if kill -0 "$old_pid" 2>/dev/null; then
				printf 'Bot did not stop gracefully; terminating it.\n' >&2
				kill -KILL "$old_pid" || true
			fi
		fi
		rm -f "$PID_FILE"
	fi
}

start_database() {
	local database_url="${DATABASE_URL:-sqlite+aiosqlite:///./twisted_services.db}"
	if [[ "$database_url" != postgresql://* && "$database_url" != postgres://* && "$database_url" != postgresql+asyncpg://* ]]; then
		printf 'Using SQLite; no database service needs starting.\n'
		return
	fi

	local container="${DB_CONTAINER_NAME:-twisted-services-db}"
	if ! command -v docker >/dev/null 2>&1; then
		printf 'DATABASE_URL points to PostgreSQL, but Docker is unavailable. Assuming PostgreSQL is managed externally.\n' >&2
		return
	fi
	if docker container inspect "$container" >/dev/null 2>&1; then
		if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != "true" ]]; then
			printf 'Starting database container %s...\n' "$container"
			docker start "$container" >/dev/null
		else
			printf 'Database container %s is already running.\n' "$container"
		fi
		return
	fi
	if [[ "${CREATE_DB_CONTAINER:-false}" != "true" ]]; then
		printf 'PostgreSQL container %s is missing; assuming PostgreSQL is managed externally. Set CREATE_DB_CONTAINER=true to create it.\n' "$container" >&2
		return
	fi

	: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required when CREATE_DB_CONTAINER=true}"
	printf 'Creating database container %s...\n' "$container"
	docker run -d --name "$container" \
		-e "POSTGRES_DB=${POSTGRES_DB:-twisted_services}" \
		-e "POSTGRES_USER=${POSTGRES_USER:-twisted_services}" \
		-e "POSTGRES_PASSWORD=$POSTGRES_PASSWORD" \
		-p "${POSTGRES_PORT:-5432}:5432" \
		"${POSTGRES_IMAGE:-postgres:16-alpine}" >/dev/null
}

start_database
stop_bot
printf 'Starting Twisted Services...\n'
nohup "$PYTHON_BIN" -m twisted_services.main >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
printf 'Bot started with pid %s. Logs: %s\n' "$(<"$PID_FILE")" "$LOG_FILE"
