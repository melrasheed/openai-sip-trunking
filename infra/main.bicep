@description('Name of the web app. Must be globally unique.')
param appName string

@description('Azure region for the App Service plan and web app.')
param location string = resourceGroup().location

@description('App Service plan SKU. B1 or higher is recommended so Always On can be enabled.')
param sku string = 'B1'

@description('Endpoint of your Azure OpenAI (Microsoft Foundry) resource, e.g. https://my-resource.openai.azure.com')
param azureOpenAiEndpoint string

@description('Name of your realtime model deployment (not the model name).')
param azureOpenAiDeployment string = 'gpt-realtime'

@description('API key for the Azure OpenAI resource.')
@secure()
param azureOpenAiApiKey string

@description('Webhook signing secret returned when the webhook endpoint was created.')
@secure()
param azureOpenAiWebhookSecret string

@description('Optional token enabling the admin call-control routes. Leave empty to keep them disabled.')
@secure()
param adminApiToken string = ''

@description('Optional Azure AI Search endpoint hosting the knowledge base, e.g. https://my-search.search.windows.net. Leave empty to run without a knowledge base.')
param azureSearchEndpoint string = ''

@description('Optional name of the Azure AI Search knowledge base (not the index).')
param azureSearchKnowledgeBase string = ''

@description('Optional admin or query key for the search service. Required for the knowledge base to be attached to calls.')
@secure()
param azureSearchApiKey string = ''

@description('Optional Azure AI Search API version. Leave empty to use the application default.')
param azureSearchApiVersion string = ''

var planName = '${appName}-plan'

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  sku: {
    name: sku
  }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: appName
  location: location
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'NODE|20-lts'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      healthCheckPath: '/health'
      // The app dials out to Azure OpenAI over WSS, and the console's web
      // calls stream the browser's audio in over a WebSocket.
      webSocketsEnabled: true
      appCommandLine: 'npm start'
      appSettings: [
        {
          name: 'AZURE_OPENAI_ENDPOINT'
          value: azureOpenAiEndpoint
        }
        {
          name: 'AZURE_OPENAI_DEPLOYMENT'
          value: azureOpenAiDeployment
        }
        {
          name: 'AZURE_OPENAI_API_KEY'
          value: azureOpenAiApiKey
        }
        {
          name: 'AZURE_OPENAI_WEBHOOK_SECRET'
          value: azureOpenAiWebhookSecret
        }
        {
          name: 'ADMIN_API_TOKEN'
          value: adminApiToken
        }
        {
          name: 'AZURE_SEARCH_ENDPOINT'
          value: azureSearchEndpoint
        }
        {
          name: 'AZURE_SEARCH_KNOWLEDGE_BASE'
          value: azureSearchKnowledgeBase
        }
        {
          name: 'AZURE_SEARCH_API_KEY'
          value: azureSearchApiKey
        }
        {
          name: 'AZURE_SEARCH_API_VERSION'
          value: azureSearchApiVersion
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
      ]
    }
  }
}

output webhookUrl string = 'https://${site.properties.defaultHostName}/webhook'
output appUrl string = 'https://${site.properties.defaultHostName}'
