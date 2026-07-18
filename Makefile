# Makefile for homelab-nl-image-gen
# Wraps common docker compose operations.

COMPOSE ?= docker compose
SERVICE ?= nl-image-gen

.DEFAULT_GOAL := help

.PHONY: help up down restart start stop build rebuild pull logs logs-app logs-mcp ps status shell config prune clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Build (if needed) and start all services in background
	$(COMPOSE) up -d

down: ## Stop and remove containers, networks
	$(COMPOSE) down

restart: ## Restart all services
	$(COMPOSE) restart

start: ## Start existing stopped containers
	$(COMPOSE) start

stop: ## Stop running containers (without removing)
	$(COMPOSE) stop

build: ## Build images
	$(COMPOSE) build

rebuild: ## Rebuild images without cache, then start
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

pull: ## Pull latest base images
	$(COMPOSE) pull

logs: ## Follow logs for all services
	$(COMPOSE) logs -f --tail=100

logs-app: ## Follow logs for the app service
	$(COMPOSE) logs -f --tail=100 nl-image-gen

logs-mcp: ## Follow logs for the mcp service
	$(COMPOSE) logs -f --tail=100 nl-image-gen-mcp

ps: ## Show container status
	$(COMPOSE) ps

status: ps ## Alias for ps

shell: ## Open a shell in a service ($(SERVICE)); override with SERVICE=name
	$(COMPOSE) exec $(SERVICE) sh

config: ## Validate and print the effective compose config
	$(COMPOSE) config

prune: ## Remove stopped containers and dangling images (system-wide)
	docker system prune -f

clean: ## Stop stack and remove volumes (DELETES gallery data)
	$(COMPOSE) down -v
