.PHONY: up down logs pull-model demo demo-outreach demo-batch ps restart doctor

# Bring up all services (build images on first run)
up:
	docker-compose up --build -d
	@echo "Gateway:  http://localhost:8080/health"
	@echo "Next: 'make pull-model' to download Qwen (first time only)."

down:
	docker-compose down

restart:
	docker-compose restart gateway ai-brain

ps:
	docker-compose ps

logs:
	docker-compose logs -f --tail=100 gateway ai-brain

# Pull the local models into the Ollama container (first run only). Pulls BOTH
# the base model (qwen2.5:3b, ~1.9GB) and the premium model used by smart routing
# (qwen3:4b-instruct-2507, ~2.5GB). qwen3:8b stays OPTIONAL/off — pull it manually
# only if you set PREMIUM_MODEL=qwen3:8b (needs ~6GB+, risky on an 8GB box).
pull-model:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	echo "Pulling base model: $${MODEL:-qwen2.5:3b}"; \
	docker-compose exec -T ollama ollama pull $${MODEL:-qwen2.5:3b}; \
	echo "Pulling premium model: $${PREMIUM_MODEL:-qwen3:4b-instruct-2507-q4_K_M}"; \
	docker-compose exec -T ollama ollama pull $${PREMIUM_MODEL:-qwen3:4b-instruct-2507-q4_K_M}
	@echo "(Optional 8B premium is OFF by default: 'docker-compose exec ollama ollama pull qwen3:8b')"

# Runtime health & resource dashboard. Shows active model strategy, the model
# ollama currently holds in RAM, host memory/load, per-container usage, queue
# state counts, every service's health, and a recommended scan limit for THIS box.
doctor:
	@echo "================= LinkedIn Copilot · doctor ================="
	@echo "--- service health ---"
	@printf "  redis    : "; docker-compose exec -T redis redis-cli ping 2>/dev/null || echo "DOWN"
	@printf "  gateway  : "; curl -s -m 3 localhost:8080/health 2>/dev/null || echo "DOWN"; echo
	@printf "  ollama   : "; (curl -s -m 3 localhost:11434/api/tags >/dev/null 2>&1 && echo "ok") || echo "DOWN"
	@printf "  ai-brain : "; cid=$$(docker-compose ps -q ai-brain 2>/dev/null); \
	  [ -n "$$cid" ] && docker inspect -f 'status={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}n/a{{end}}' $$cid 2>/dev/null || echo "not created"
	@echo
	@echo "--- model strategy + queue (gateway /stats) ---"
	@curl -s -m 3 localhost:8080/stats 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (gateway /stats unavailable — is the stack up?)"
	@echo
	@echo "--- ollama: models loaded in RAM right now ---"
	@docker-compose exec -T ollama ollama ps 2>/dev/null || echo "  (ollama not running)"
	@echo
	@echo "--- host memory / CPU load ---"
	@free -h 2>/dev/null | awk 'NR==1||/Mem:/' || true
	@echo "  loadavg:$$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
	@echo
	@echo "--- per-container usage ---"
	@ids=$$(docker-compose ps -q 2>/dev/null); [ -n "$$ids" ] && docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' $$ids 2>/dev/null || echo "  (no running containers)"
	@echo
	@echo "--- recommended scan limit (this box) ---"
	@free -m 2>/dev/null | awk '/Mem:/ {a=$$7} END { if (a=="" ) print "  2  (could not read free RAM)"; else if (a<1200) print "  1  (only "a"MB free — defensive mode likely; keep batches tiny)"; else if (a<2600) print "  1-2  ("a"MB free)"; else print "  2  ("a"MB free — comfortable)" }'
	@echo "============================================================"

# End-to-end smoke test WITHOUT the browser: ingest -> wait -> show draft -> approve.
demo:
	@echo "== POST /ingest =="
	curl -s -X POST localhost:8080/ingest -H 'Content-Type: application/json' \
	  -d '{"thread_id":"demo","text":"Hi! Are you free to chat Thursday about the role?"}' ; echo
	@echo "== waiting for AI draft (Ollama generation can take a while) =="
	@for i in $$(seq 1 60); do \
	  s=$$(curl -s localhost:8080/draft/demo); \
	  echo "$$s" | grep -q PENDING_APPROVAL && { echo "$$s"; break; }; \
	  sleep 2; \
	done
	@echo "== POST /approve =="
	curl -s -X POST localhost:8080/approve -H 'Content-Type: application/json' \
	  -d '{"thread_id":"demo","decision":"approve"}' ; echo
	@echo "(In the browser this text is INJECTED into the composer. A human still clicks Send.)"

# Phase 1 — browserless AI Outreach end-to-end: profile-ingest -> generate -> approve.
demo-outreach:
	@echo "== POST /profile-ingest =="
	curl -s -X POST localhost:8080/profile-ingest -H 'Content-Type: application/json' \
	  -d '{"profile_id":"demo-prof","headline":"Founder & CEO at Acme Robotics","about":"Building warehouse automation. Ex-Tesla.","profile_text":"Name: Jane Doe","context":"We both attended RoboConf 2025"}' ; echo
	@echo "== waiting for AI outreach (Ollama generation can take a while) =="
	@for i in $$(seq 1 60); do \
	  s=$$(curl -s localhost:8080/outreach/demo-prof); \
	  echo "$$s" | grep -q OUTREACH_READY && { echo "$$s"; break; }; \
	  sleep 2; \
	done
	@echo "== POST /approve-outreach =="
	curl -s -X POST localhost:8080/approve-outreach -H 'Content-Type: application/json' \
	  -d '{"profile_id":"demo-prof","decision":"approve"}' ; echo
	@echo "(In the browser this note is INJECTED into the connection modal. A human still clicks Connect.)"

# Phase 2 — browserless Batch Outreach Queue: bulk ingest -> wait -> approve_all.
demo-batch:
	@echo "== POST /batch-ingest (3 profiles) =="
	curl -s -X POST localhost:8080/batch-ingest -H 'Content-Type: application/json' -d '{"batch_id":"demo-batch","limit":3,"filters":"founder,sales","profiles":[{"profile_id":"demo-b1","name":"Ayesha Khan","headline":"Founder & CEO at Loom Textiles","company":"Loom","location":"Karachi","mutual":"6 mutual connections"},{"profile_id":"demo-b2","name":"Rohan Mehta","headline":"VP Sales | SaaS | B2B","company":"Cloudify","location":"Bangalore","mutual":""},{"profile_id":"demo-b3","name":"Daniel Park","headline":"Co-founder at Northwind Robotics","company":"Northwind","location":"Seoul","mutual":""}]}' ; echo
	@echo "== waiting for all drafts (Ollama generation can take a while) =="
	@for i in $$(seq 1 120); do \
	  s=$$(curl -s localhost:8080/batch/demo-batch); \
	  echo "$$s" | grep -q OUTREACH_GENERATING || { echo "$$s" | python3 -m json.tool; break; }; \
	  sleep 3; \
	done
	@echo "== POST /batch-approve approve_all =="
	curl -s -X POST localhost:8080/batch-approve -H 'Content-Type: application/json' \
	  -d '{"batch_id":"demo-batch","decision":"approve_all"}' ; echo
	@echo "(In the browser each approved note is INJECTED per-profile. A human still clicks Connect.)"
