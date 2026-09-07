// Infraestrutura do próprio SAI (SPEC.md seção 12).
//
// Provisiona: Log Analytics + Application Insights (observabilidade do próprio
// sistema), Key Vault (segredos — SPEC 10.3), PostgreSQL Flexible Server com
// pgvector, Azure Container Registry, e um Container App rodando a API.
//
// O Container App recebe uma Managed Identity com acesso de leitura ao Key
// Vault, de forma que NENHUM segredo precisa existir como variável de
// ambiente em texto plano (SPEC 10.3).
//
// Deploy:
//   az deployment group create -g <rg> -f infra/main.bicep -p infra/main.<env>.bicepparam

targetScope = 'resourceGroup'

@description('Nome curto do ambiente: dev | staging | prod')
@allowed(['dev', 'staging', 'prod'])
param environmentName string

@description('Região do Azure para todos os recursos.')
param location string = resourceGroup().location

@description('Prefixo dos nomes dos recursos.')
param namePrefix string = 'sai'

@description('Administrador do PostgreSQL.')
param postgresAdminUser string = 'saiadmin'

@description('Senha do administrador do PostgreSQL. Passe via --parameters do CLI a partir do Key Vault, NUNCA commitada.')
@secure()
param postgresAdminPassword string

@description('Object ID (Entra) do grupo de administradores que terá acesso ao Key Vault.')
param adminGroupObjectId string

@description('Tag da imagem de container a implantar.')
param imageTag string = 'latest'

@description('SKU do PostgreSQL. prod usa uma SKU maior por padrão.')
param postgresSkuName string = environmentName == 'prod' ? 'Standard_D2ds_v4' : 'Standard_B1ms'

@description('Número mínimo de réplicas do Container App. prod mantém 1 para evitar cold start no horário comercial.')
param minReplicas int = environmentName == 'prod' ? 1 : 0

var suffix = '${namePrefix}-${environmentName}'
var uniqueSuffix = uniqueString(resourceGroup().id, environmentName)

// ---------------------------------------------------------------------------
// Observabilidade do próprio SAI (SPEC 12 — "o sistema que monitora precisa
// ser monitorado")
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: environmentName == 'prod' ? 90 : 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ---------------------------------------------------------------------------
// Key Vault — todos os segredos (SPEC 10.3)
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-${namePrefix}-${environmentName}-${take(uniqueSuffix, 6)}'
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    // Proteção contra exclusão acidental/maliciosa dos segredos em produção.
    enablePurgeProtection: environmentName == 'prod' ? true : null
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// PostgreSQL Flexible Server (pgvector habilitado)
// ---------------------------------------------------------------------------

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2023-06-01-preview' = {
  name: 'psql-${suffix}-${take(uniqueSuffix, 6)}'
  location: location
  sku: {
    name: postgresSkuName
    tier: environmentName == 'prod' ? 'GeneralPurpose' : 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminUser
    administratorLoginPassword: postgresAdminPassword
    storage: { storageSizeGB: environmentName == 'prod' ? 128 : 32 }
    backup: {
      // SPEC 11: backup diário do banco do próprio sistema, retenção 30 dias.
      backupRetentionDays: 30
      geoRedundantBackup: environmentName == 'prod' ? 'Enabled' : 'Disabled'
    }
    highAvailability: {
      mode: environmentName == 'prod' ? 'ZoneRedundant' : 'Disabled'
    }
  }
}

resource postgresDb 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-06-01-preview' = {
  parent: postgres
  name: 'sai'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Habilita a extensão pgvector (necessária para knowledge_chunks.embedding).
resource pgvectorConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2023-06-01-preview' = {
  parent: postgres
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
}

// ---------------------------------------------------------------------------
// Container Registry + Container App
// ---------------------------------------------------------------------------

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${namePrefix}${environmentName}${take(uniqueSuffix, 6)}'
  location: location
  sku: { name: 'Basic' }
  properties: { adminUserEnabled: false }
}

resource containerAppEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${suffix}'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-${suffix}-api'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerAppEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'sai-api'
          image: '${acr.properties.loginServer}/sai:${imageTag}'
          resources: {
            cpu: json(environmentName == 'prod' ? '1.0' : '0.5')
            memory: environmentName == 'prod' ? '2Gi' : '1Gi'
          }
          env: [
            // Apenas referências não-sensíveis; os segredos são lidos do Key
            // Vault em runtime via Managed Identity (SPEC 10.3).
            { name: 'ENVIRONMENT', value: environmentName }
            { name: 'KEY_VAULT_URI', value: keyVault.properties.vaultUri }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
          ]
          probes: [
            {
              // Aponta para /health/ready (só verifica o banco), NÃO para
              // /health: este último consulta Zabbix, F5, SQL Server etc., o
              // que é lento e faria uma indisponibilidade de terceiro tirar
              // as instâncias do SAI de rotação — justo quando a equipe mais
              // precisa da ferramenta.
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Liveness'
              httpGet: { path: '/health/ready', port: 8000 }
              initialDelaySeconds: 30
              periodSeconds: 60
              timeoutSeconds: 5
              failureThreshold: 5
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: environmentName == 'prod' ? 5 : 2
      }
    }
  }
}

// ---------------------------------------------------------------------------
// RBAC — least privilege (SPEC 10.1)
// ---------------------------------------------------------------------------

// Built-in role IDs
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'
var keyVaultAdminRoleId = '00482a5a-887f-4fb3-b363-3b7fe8e74483'
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

// A aplicação só LÊ segredos — nunca os grava nem os apaga.
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, api.id, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Somente o grupo de administradores humanos administra os segredos.
resource kvAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, adminGroupObjectId, keyVaultAdminRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultAdminRoleId)
    principalId: adminGroupObjectId
    principalType: 'Group'
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: acr
  name: guid(acr.id, api.id, acrPullRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: api.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Saídas
// ---------------------------------------------------------------------------

output apiFqdn string = api.properties.configuration.ingress.fqdn
output keyVaultUri string = keyVault.properties.vaultUri
output acrLoginServer string = acr.properties.loginServer
output postgresFqdn string = postgres.properties.fullyQualifiedDomainName
output apiPrincipalId string = api.identity.principalId
