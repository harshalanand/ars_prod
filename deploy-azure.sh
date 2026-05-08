#!/bin/bash
# ============================================================================
# ARS Azure Deployment — One-Click Setup
# ============================================================================
# 
# PREREQUISITES:
#   1. Azure CLI installed:  https://aka.ms/installazurecli
#   2. Docker installed:     https://docs.docker.com/get-docker/
#   3. Git installed
#
# USAGE:
#   chmod +x deploy-azure.sh
#   ./deploy-azure.sh
#
# This script creates:
#   - Azure Resource Group
#   - Azure Container Registry (ACR)
#   - Azure SQL Server + 2 databases (Claude + Rep_data)
#   - Azure App Service (Linux container)
#   - Azure Static Web App (frontend)
#   - All environment variables configured
#
# Total time: ~15-20 minutes
# Estimated cost: ₹15,000-25,000/month
# ============================================================================

set -e  # Exit on any error

# ── Configuration ────────────────────────────────────────────────────────
RESOURCE_GROUP="rg-ars-prod"
LOCATION="centralindia"
SQL_SERVER_NAME="ars-sql-$(date +%s | tail -c 6)"   # Unique name
SQL_ADMIN_USER="arsadmin"
SQL_ADMIN_PASS="ArsStr0ng@Pass2026!"                  # Change this!
ACR_NAME="arsacr$(date +%s | tail -c 6)"              # Unique name
APP_SERVICE_PLAN="asp-ars-prod"
BACKEND_APP_NAME="ars-api-$(date +%s | tail -c 6)"    # Unique name
JWT_SECRET=$(openssl rand -base64 48)                  # Auto-generated
SUPER_ADMIN_PASS="Admin@Ars2026!"                      # Change this!

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}"
echo "============================================"
echo "  ARS Azure Deployment"
echo "  Auto Replenishment System v2.1"
echo "============================================"
echo -e "${NC}"

# ── Step 0: Login ────────────────────────────────────────────────────────
echo -e "${YELLOW}Step 0: Azure Login${NC}"
echo "If a browser opens, sign in with your Azure account."
az login --only-show-errors 2>/dev/null || {
    echo -e "${RED}Azure login failed. Install Azure CLI: https://aka.ms/installazurecli${NC}"
    exit 1
}
echo -e "${GREEN}✓ Logged in to Azure${NC}"

# Show available subscriptions
echo ""
echo "Available subscriptions:"
az account list --output table --query "[].{Name:name, ID:id, Default:isDefault}"
echo ""
read -p "Press Enter to use the default subscription, or type a subscription ID: " SUB_ID
if [ -n "$SUB_ID" ]; then
    az account set --subscription "$SUB_ID"
    echo -e "${GREEN}✓ Using subscription: $SUB_ID${NC}"
fi

# ── Step 1: Resource Group ───────────────────────────────────────────────
echo ""
echo -e "${YELLOW}Step 1: Creating Resource Group${NC}"
az group create --name $RESOURCE_GROUP --location $LOCATION --output none
echo -e "${GREEN}✓ Resource group: $RESOURCE_GROUP ($LOCATION)${NC}"

# ── Step 2: Azure SQL Server + Databases ─────────────────────────────────
echo ""
echo -e "${YELLOW}Step 2: Creating Azure SQL Server + Databases${NC}"
echo "  This takes 2-3 minutes..."

az sql server create \
    --name $SQL_SERVER_NAME \
    --resource-group $RESOURCE_GROUP \
    --location $LOCATION \
    --admin-user $SQL_ADMIN_USER \
    --admin-password "$SQL_ADMIN_PASS" \
    --output none

# Allow Azure services
az sql server firewall-rule create \
    --resource-group $RESOURCE_GROUP \
    --server $SQL_SERVER_NAME \
    --name AllowAzureServices \
    --start-ip-address 0.0.0.0 \
    --end-ip-address 0.0.0.0 \
    --output none

# Allow current IP (for running schema scripts)
MY_IP=$(curl -s ifconfig.me)
az sql server firewall-rule create \
    --resource-group $RESOURCE_GROUP \
    --server $SQL_SERVER_NAME \
    --name MyIP \
    --start-ip-address $MY_IP \
    --end-ip-address $MY_IP \
    --output none

# Create Claude database (System DB — S1 tier)
az sql db create \
    --resource-group $RESOURCE_GROUP \
    --server $SQL_SERVER_NAME \
    --name Claude \
    --service-objective S1 \
    --max-size 2GB \
    --output none

# Create Rep_data database (Business DB — S2 tier for MSA calculations)
az sql db create \
    --resource-group $RESOURCE_GROUP \
    --server $SQL_SERVER_NAME \
    --name Rep_data \
    --service-objective S2 \
    --max-size 20GB \
    --output none

SQL_FQDN="$SQL_SERVER_NAME.database.windows.net"
echo -e "${GREEN}✓ SQL Server: $SQL_FQDN${NC}"
echo -e "${GREEN}✓ Databases: Claude (S1), Rep_data (S2)${NC}"

# ── Step 3: Container Registry ───────────────────────────────────────────
echo ""
echo -e "${YELLOW}Step 3: Creating Container Registry${NC}"
az acr create \
    --resource-group $RESOURCE_GROUP \
    --name $ACR_NAME \
    --sku Basic \
    --admin-enabled true \
    --output none

ACR_LOGIN_SERVER="$ACR_NAME.azurecr.io"
ACR_PASSWORD=$(az acr credential show --name $ACR_NAME --query "passwords[0].value" -o tsv)
echo -e "${GREEN}✓ Container Registry: $ACR_LOGIN_SERVER${NC}"

# ── Step 4: Build & Push Backend Docker Image ────────────────────────────
echo ""
echo -e "${YELLOW}Step 4: Building & Pushing Backend Docker Image${NC}"
echo "  This takes 3-5 minutes (installs ODBC driver + Python deps)..."

cd backend
az acr build \
    --registry $ACR_NAME \
    --image ars-backend:latest \
    --image ars-backend:v2.1 \
    . --no-logs 2>/dev/null || \
az acr build \
    --registry $ACR_NAME \
    --image ars-backend:latest \
    --image ars-backend:v2.1 \
    .
cd ..
echo -e "${GREEN}✓ Backend image pushed: $ACR_LOGIN_SERVER/ars-backend:latest${NC}"

# ── Step 5: App Service Plan + Web App ───────────────────────────────────
echo ""
echo -e "${YELLOW}Step 5: Creating App Service (Backend)${NC}"
echo "  Using B2 tier (2 cores, 3.5GB RAM) for parallel MSA processing..."

az appservice plan create \
    --name $APP_SERVICE_PLAN \
    --resource-group $RESOURCE_GROUP \
    --is-linux \
    --sku B2 \
    --location $LOCATION \
    --output none

az webapp create \
    --resource-group $RESOURCE_GROUP \
    --plan $APP_SERVICE_PLAN \
    --name $BACKEND_APP_NAME \
    --container-image-name "$ACR_LOGIN_SERVER/ars-backend:latest" \
    --container-registry-url "https://$ACR_LOGIN_SERVER" \
    --container-registry-user $ACR_NAME \
    --container-registry-password "$ACR_PASSWORD" \
    --output none

# Configure environment variables
az webapp config appsettings set \
    --resource-group $RESOURCE_GROUP \
    --name $BACKEND_APP_NAME \
    --settings \
        APP_ENV=production \
        DB_SERVER="$SQL_FQDN" \
        DB_NAME=Claude \
        DATA_DB_NAME=Rep_data \
        DB_USERNAME="$SQL_ADMIN_USER" \
        DB_PASSWORD="$SQL_ADMIN_PASS" \
        DB_DRIVER="ODBC Driver 18 for SQL Server" \
        DB_TRUST_CERT=no \
        DB_ENCRYPT=yes \
        DB_POOL_SIZE=15 \
        DB_MAX_OVERFLOW=25 \
        DB_POOL_RECYCLE=300 \
        JWT_SECRET_KEY="$JWT_SECRET" \
        JWT_ACCESS_TOKEN_EXPIRE_MINUTES=480 \
        CORS_ORIGINS='["https://'"$BACKEND_APP_NAME"'.azurewebsites.net","http://localhost:3000"]' \
        SUPER_ADMIN_USERNAME=superadmin \
        SUPER_ADMIN_EMAIL=admin@nubo.in \
        SUPER_ADMIN_PASSWORD="$SUPER_ADMIN_PASS" \
        LOG_LEVEL=INFO \
        LOG_TO_FILE=false \
        WEBSITES_PORT=8000 \
    --output none

# Enable always-on to prevent cold starts
az webapp config set \
    --resource-group $RESOURCE_GROUP \
    --name $BACKEND_APP_NAME \
    --always-on true \
    --output none

BACKEND_URL="https://$BACKEND_APP_NAME.azurewebsites.net"
echo -e "${GREEN}✓ Backend deployed: $BACKEND_URL${NC}"

# ── Step 6: Build & Deploy Frontend ──────────────────────────────────────
echo ""
echo -e "${YELLOW}Step 6: Building Frontend${NC}"

cd frontend

# Build frontend with API URL pointing to backend
export VITE_API_URL="$BACKEND_URL/api/v1"
npm ci --silent 2>/dev/null || npm install --silent
npm run build

cd ..
echo -e "${GREEN}✓ Frontend built${NC}"

# Deploy frontend as a second container or static files
# Option: Deploy to same App Service as a static site behind nginx
echo ""
echo -e "${YELLOW}Step 6b: Deploying Frontend to App Service${NC}"

cd frontend
az acr build \
    --registry $ACR_NAME \
    --image ars-frontend:latest \
    --build-arg VITE_API_URL=/api/v1 \
    . --no-logs 2>/dev/null || \
az acr build \
    --registry $ACR_NAME \
    --image ars-frontend:latest \
    --build-arg VITE_API_URL=/api/v1 \
    .
cd ..

# Create frontend app service
FRONTEND_APP_NAME="ars-web-$(echo $BACKEND_APP_NAME | grep -o '[0-9]*$')"
az webapp create \
    --resource-group $RESOURCE_GROUP \
    --plan $APP_SERVICE_PLAN \
    --name $FRONTEND_APP_NAME \
    --container-image-name "$ACR_LOGIN_SERVER/ars-frontend:latest" \
    --container-registry-url "https://$ACR_LOGIN_SERVER" \
    --container-registry-user $ACR_NAME \
    --container-registry-password "$ACR_PASSWORD" \
    --output none

FRONTEND_URL="https://$FRONTEND_APP_NAME.azurewebsites.net"
# Update CORS to include frontend URL
az webapp config appsettings set \
    --resource-group $RESOURCE_GROUP \
    --name $BACKEND_APP_NAME \
    --settings \
        CORS_ORIGINS='["'"$FRONTEND_URL"'","'"$BACKEND_URL"'","http://localhost:3000"]' \
    --output none

echo -e "${GREEN}✓ Frontend deployed: $FRONTEND_URL${NC}"

# ── Step 7: Run Database Schema ──────────────────────────────────────────
echo ""
echo -e "${YELLOW}Step 7: Running Database Schema Scripts${NC}"
echo "  Creating tables in Claude and Rep_data databases..."

# Check if sqlcmd is available
if command -v sqlcmd &> /dev/null; then
    # Run Claude DB schema (skip CREATE DATABASE section — DB already exists)
    sqlcmd -S "$SQL_FQDN" -U "$SQL_ADMIN_USER" -P "$SQL_ADMIN_PASS" \
           -d Claude -i backend/scripts/create_claude_db.sql \
           -b 2>/dev/null && echo -e "${GREEN}  ✓ Claude schema created${NC}" || \
           echo -e "${YELLOW}  ⚠ Claude schema — run manually in SSMS (see below)${NC}"

    # Run Rep_data DB schema
    sqlcmd -S "$SQL_FQDN" -U "$SQL_ADMIN_USER" -P "$SQL_ADMIN_PASS" \
           -d Rep_data -i backend/scripts/create_rep_data_db.sql \
           -b 2>/dev/null && echo -e "${GREEN}  ✓ Rep_data schema created${NC}" || \
           echo -e "${YELLOW}  ⚠ Rep_data schema — run manually in SSMS (see below)${NC}"
else
    echo -e "${YELLOW}  sqlcmd not found. Run these scripts manually:${NC}"
    echo "  Connect to: $SQL_FQDN"
    echo "  Username:   $SQL_ADMIN_USER"
    echo "  Password:   $SQL_ADMIN_PASS"
    echo ""
    echo "  1. Open SSMS or Azure Data Studio"
    echo "  2. Connect to $SQL_FQDN"
    echo "  3. Run: backend/scripts/create_claude_db.sql (on Claude DB)"
    echo "  4. Run: backend/scripts/create_rep_data_db.sql (on Rep_data DB)"
    echo "  5. Run: backend/scripts/015_add_category_rls.sql (on Claude DB)"
fi

# ── Step 8: Wait for deployment ──────────────────────────────────────────
echo ""
echo -e "${YELLOW}Step 8: Waiting for deployment to start...${NC}"
sleep 30

# Health check
echo "Checking backend health..."
for i in {1..10}; do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BACKEND_URL/health" 2>/dev/null)
    if [ "$HTTP_CODE" = "200" ]; then
        echo -e "${GREEN}✓ Backend is healthy!${NC}"
        curl -s "$BACKEND_URL/health" | python3 -m json.tool 2>/dev/null || curl -s "$BACKEND_URL/health"
        break
    fi
    echo "  Waiting... ($i/10) — HTTP $HTTP_CODE"
    sleep 15
done

# ══════════════════════════════════════════════════════════════════════════
# DONE
# ══════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BLUE}============================================${NC}"
echo -e "${GREEN}  ARS Deployment Complete!${NC}"
echo -e "${BLUE}============================================${NC}"
echo ""
echo -e "  ${GREEN}Frontend:${NC}  $FRONTEND_URL"
echo -e "  ${GREEN}API Docs:${NC}  $BACKEND_URL/docs"
echo -e "  ${GREEN}Health:${NC}    $BACKEND_URL/health"
echo ""
echo -e "  ${GREEN}Login:${NC}"
echo -e "    Username: superadmin"
echo -e "    Password: $SUPER_ADMIN_PASS"
echo ""
echo -e "  ${GREEN}SQL Server:${NC}"
echo -e "    Server:   $SQL_FQDN"
echo -e "    Username: $SQL_ADMIN_USER"
echo -e "    Password: $SQL_ADMIN_PASS"
echo -e "    Databases: Claude, Rep_data"
echo ""
echo -e "  ${GREEN}Pipeline (parallel MSA — replaces 20 machines):${NC}"
echo -e "    POST $BACKEND_URL/api/v1/pipeline/run"
echo -e "    GET  $BACKEND_URL/api/v1/pipeline/status"
echo ""
echo -e "  ${YELLOW}IMPORTANT — Next Steps:${NC}"
echo -e "    1. Run schema scripts if sqlcmd wasn't available (see Step 7)"
echo -e "    2. Migrate data from HOPC560 to Azure SQL (BACPAC export/import)"
echo -e "    3. Share the Frontend URL with your team"
echo ""
echo -e "  ${YELLOW}Save these credentials — they won't be shown again!${NC}"

# Save credentials to a file
cat > deployment-credentials.txt << EOF
ARS Azure Deployment Credentials
================================
Generated: $(date)

Frontend URL:    $FRONTEND_URL
API URL:         $BACKEND_URL
API Docs:        $BACKEND_URL/docs
Health Check:    $BACKEND_URL/health

Login:
  Username: superadmin
  Password: $SUPER_ADMIN_PASS

SQL Server:
  Server:   $SQL_FQDN
  Username: $SQL_ADMIN_USER
  Password: $SQL_ADMIN_PASS

Azure Resources:
  Resource Group:    $RESOURCE_GROUP
  SQL Server:        $SQL_SERVER_NAME
  Container Registry: $ACR_NAME
  Backend App:       $BACKEND_APP_NAME
  Frontend App:      $FRONTEND_APP_NAME
  App Service Plan:  $APP_SERVICE_PLAN

JWT Secret: $JWT_SECRET

Pipeline Endpoint: POST $BACKEND_URL/api/v1/pipeline/run
EOF

echo ""
echo -e "${GREEN}Credentials saved to: deployment-credentials.txt${NC}"
echo ""
