# Dependencias

## Python versionado

| Dependencia | Versao | Uso |
|---|---:|---|
| FastAPI | 0.115.12 | HTTP/API |
| Uvicorn | 0.34.2 | servidor ASGI |
| SQLAlchemy | 2.0.41 | acesso async a dados |
| asyncpg | 0.30.0 | driver PostgreSQL |
| greenlet | 3.2.4 | bridge SQLAlchemy |
| Celery | 5.4.0 | tasks/beat |
| Redis client | 5.0.7 | backend, heartbeat e locks |
| Paramiko | 3.4.1 | SFTP |
| pytest | 8.3.5 | testes |
| pytest-asyncio | 0.26.0 | testes async |

Runtime documentado: Python 3.12+.

## Infraestrutura

- PostgreSQL/PgBouncer.
- RabbitMQ/AMQP para broker.
- Redis para backend/heartbeat/locks.
- Target Core e Files API via HTTP.
- SFTP para `generate_file`.

## Dependencias implicitas

- Node.js e necessario para `code_editor`; nao esta declarado em `requirements.txt` nem garantido pelos templates systemd.
- `rg` e usado por `scripts/dev_phase_stack.sh` para detectar workers prontos; nao esta documentado como pre-requisito e estava ausente no host do onboarding.
- `curl`, shell Bash/Zsh, `launchctl` no macOS e `systemctl/journalctl` no Linux.
- Tablespaces de workspace precisam existir para a migration `0009`.

## Politica

Nao atualizar dependencias durante sustentacao sem necessidade operacional, analise de compatibilidade e rollback. Modernizacao ampla pertence a V2.

