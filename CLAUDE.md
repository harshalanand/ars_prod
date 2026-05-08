# CLAUDE.md — Instructions for Claude Code + Claude Desktop

## UNIVERSAL MCP SERVER (connect this first — gives Claude live access to all systems)
```
URL:  https://universal-mcp.akash-bab.workers.dev
Key:  stored as MCP_API_KEY in GitHub Secrets
Transport: HTTP POST JSON-RPC
Tools: 15 (ars_dashboard, ars_allocations, ars_msa_sequences, ars_stores, ars_users,
           ars_audit, ars_jobs, ars_checklist, ars_table, v2_infra_status,
           v2_rfc_health, nubo_status, github_repos, github_file, get_all_context)
```

Claude Desktop (~/.claude/claude_desktop_config.json):
```json
{
  "mcpServers": {
    "v2": {
      "url": "https://universal-mcp.akash-bab.workers.dev",
      "headers": { "X-API-Key": "[MCP_API_KEY from setup doc]" }
    }
  }
}
```

## PROJECT OVERVIEW
V2 Retail Auto Replenishment System. 320+ stores, 242 MAJCATs, replaces 20-machine Excel process.
Owner: Akash Agarwal, Director V2 Retail. Repo: https://github.com/harshalanand/ars

## HOW TO DEPLOY (every code change)
```bash
TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/[AZURE_TENANT]/oauth2/v2.0/token" \
  -d "client_id=[AZURE_CLIENT]" -d "client_secret=[AZURE_SECRET_IN_ENV]" \
  -d "scope=https://management.azure.com/.default" -d "grant_type=client_credentials" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)[access_token])")
cd backend
zip -r /tmp/ars-deploy.zip . -x "__pycache__/*" "venv/*" "logs/*" "*.pyc" ".env"
curl -X POST "https://ars-v2retail-api.scm.azurewebsites.net/api/zipdeploy?isAsync=true" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/zip" --data-binary @/tmp/ars-deploy.zip
sleep 120 && curl -s "https://ars-v2retail-api.azurewebsites.net/health"
```

## CREDENTIALS (full values in V2_RETAIL_HANDOVER.md and DEVELOPER_CLAUDE_SETUP.md)
- Azure API:    https://ars-v2retail-api.azurewebsites.net | superadmin / Admin@12345
- Azure SQL:    ars-v2retail-sql.database.windows.net | arsadmin / [SQL_PASS_IN_ENV]
- Azure Tenant: 3eb968d0-bf19-40f9-b191-f3186ac38f02
- Azure Client: 8f54a771-3b04-4458-bef3-f1fa98dc38a0 | Secret: [AZURE_SECRET_IN_ENV]
- Azure Sub:    7c2e7784-61b3-4aa7-9967-f41b381406dd | RG: rg-ars-prod
- Snowflake:    iafphkw-hh80816 | akashv2kart | [SF_PASS_IN_ENV]
- Supabase:     https://pymdqnnwwxrgeolvgvgv.supabase.co
- Cloudflare:   Account bab06c93e17ae71cae3c11b4cc40240b | Token: [CF_TOKEN_IN_ENV]
- RFC Key:      v2-rfc-proxy-2026 (Header: X-RFC-Key)
- ABAP Studio:  akash / admin2026

## CLOUDFLARE (34 workers)
V2:   v2-replenishment, v2-central-api, v2-data-api, v2-hht-dashboard, v2-orchestrator,
      v2-planning-hub-api, v2-project-hub, v2-rfc-pipeline, v2-sql-analyst, v2-sql-studio,
      v2-sync-engine, abap-ai-studio
HHT:  hht-proxy, hht-fleet-dashboard, hht-health-monitor, hht-rate-limiter, hht-rfc-cache,
      hht-stats-aggregator, hht-log-forwarder, hht-error-alerter, hht-apk-publisher
Nubo: nubo-hub, nubo-ads-dashboard, nubo-ads-bot, nubo-ads-cron, nubo-ads-snapshot,
      nubo-ga-bot, nubo-ig-fetch, nubo-marketing-dashboard, nubo-meta-proxy,
      nubo-n8n-proxy, nubo-smart-proxy, nubo-social

## ARS DATABASES
Claude DB:   rbac_roles, rbac_users, rbac_permissions, rls_stores, audit_log
Rep_data DB: store_stock, store_sales, alloc_header, alloc_detail, retail_gen_article,
             retail_variant_article, ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART,
             MSA_Calculation_Sequence, Cont_presets, ARS_CHECKLIST

## MSA ALGORITHM (9 steps in msa_service.py)
filter SLOC → normalize → fill dims → SEG=[APP,GM] → pivot by SLOC
→ merge MASTER_ALC_PEND → FNL_Q=max(STK-PEND,0) → gen color variants → aggregate
Output: ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART (ST_CD renamed to RDC)

## RECENT CHANGES (April 2026)
- Universal MCP deployed: https://universal-mcp.akash-bab.workers.dev (15 tools)
- MSA tables: cl_msa→ARS_MSA_TOTAL, cl_generated_color→ARS_MSA_GEN_ART
- ST_CD renamed to RDC in all MSA output
- listing.py import: app.core.database → app.database.session

