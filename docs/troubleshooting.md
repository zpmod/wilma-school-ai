# Troubleshooting

## Parser Issues

### "Parser unhealthy" / binary_sensor.wilma_parser_healthy is off

1. **Check if the parser container is running:**
   ```bash
   podman ps | grep wilma-parser
   ```
2. **Check parser logs:**
   ```bash
   podman logs wilma-parser --tail 50
   ```
3. **Test connectivity from HA:**
   ```bash
   curl http://localhost:8090/healthz
   ```

### Parser says `model_loaded: false`

Ollama is reachable but the model isn't pulled yet:
```bash
ollama pull hf.co/mradermacher/Llama-Poro-2-8B-Instruct-GGUF:Q4_K_M
```

Wait for the download (~4.9 GB), then check again:
```bash
curl http://localhost:8090/healthz
```

### Parse takes very long (>30 minutes)

On Raspberry Pi 5, typical parse times are 5–20 minutes. If it's consistently longer:
- Check CPU throttling: `vcgencmd measure_temp` (throttles at 85°C)
- Ensure adequate cooling (active fan recommended)
- Check if other processes are competing for RAM
- Consider reducing `WILMA_PARSER_NUM_CTX` from 8192 to 4096

### Events not appearing in calendar

1. Check `sensor.wilma_parser_unsynced` — are events stuck?
2. Verify the sync automation is running (triggers every 3 minutes)
3. Check HA logs for `rest_command.sync_wilma_events` errors
4. Manual sync: call `rest_command.sync_wilma_events` from Developer Tools

## Integration Issues

### "Cannot connect" during setup

- Verify the Wilma URL format: `https://yourschool.inschool.fi`
- Check that the URL is reachable from your HA instance
- Some schools use custom domains — check your login URL in a browser

### "Invalid auth" / login fails

- Wilma passwords are case-sensitive
- Some schools have separate teacher/parent portals — use the parent one
- Try logging in via browser first to confirm credentials work
- If you have MFA enabled on Wilma, this integration may not work (not supported)

### "No children found"

- The account must have at least one child linked
- Guardian accounts that are pending approval won't show children
- Check that you can see children when logging in via browser

### Sensors show "unavailable"

- The integration polls every 4 hours by default
- After first setup, wait for the first poll cycle
- Check HA logs for `custom_components.wilma_school_ai` errors
- Wilma sessions expire — the integration re-logins automatically, but network issues can cause temporary unavailability

## Network Issues

### Host networking vs bridge (Podman)

**Bridge mode** (default): Parser URL uses container name:
```yaml
# In HA rest_commands or secrets:
wilma_parser_url: "http://wilma-parser:8090"
```

**Host mode** (recommended for Pi): Parser URL uses localhost:
```yaml
wilma_parser_url: "http://localhost:8090"
```

Choose based on your container setup. Host mode is simpler on Raspberry Pi.

### Ollama on a different machine

If Ollama runs on a separate server:
```yaml
# docker-compose.yml environment
environment:
  - OLLAMA_BASE_URL=http://<OLLAMA_HOST_IP>:11434
```

Ensure the Ollama server allows remote connections (`OLLAMA_HOST=0.0.0.0`).

## Performance Tuning

### Reducing memory usage

If RAM is tight, reduce context window:
```yaml
environment:
  - WILMA_PARSER_NUM_CTX=4096   # default: 8192
  - WILMA_PARSER_NUM_PREDICT=1000  # default: 2000
```

Trade-off: very long messages may be truncated.

### Using a different model

Any Ollama-compatible model works. Set via environment:
```yaml
environment:
  - WILMA_PARSER_MODEL=llama3:8b
```

Note: non-Finnish models will likely have lower recall on Finnish dates and school terminology.

## Clearing Parser Cache

If you need to re-parse all messages (e.g., after a prompt update):
```bash
# Stop parser
docker-compose stop wilma-parser

# Delete the database
rm services/parser/data/parser.db

# Restart — reconciliation will re-parse everything
docker-compose start wilma-parser
```

The reconciliation automation will re-send all known messages within an hour.
