# Operations

- The shared bridge (`com.kash.telegrambridge`) owns inbound polling for the approved Kash bot.
- Kash remains a read-only data core. Do not add write/system-command paths through the bridge.
- Do not start a second Kash Telegram poller locally.
- Provider behavior belongs to Kash's own conversational layer; do not introduce local Ollama for interactive requests.
