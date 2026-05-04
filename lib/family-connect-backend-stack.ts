import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { WebSocketLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2_authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';

export class FamilyConnectBackendStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // Connection management table (Stores active WebSocket connection IDs)
    const connectionsTable = new dynamodb.Table(this, 'ConnectionsTable', {
      tableName: 'Connections',
      partitionKey: { name: 'connectionId', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Chat history table (Partitioned by roomId, sorted by timestamp for chronological retrieval)
    const chatHistoryTable = new dynamodb.Table(this, 'ChatHistoryTable', {
      tableName: 'ChatHistory',
      partitionKey: { name: 'roomId', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'timestamp', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ------------------------------------------------------------------
    // NEW: S3 Bucket for Multi-modal Image Uploads
    // Rationale: Facilitates presigned URL uploads directly from the client,
    // bypassing API Gateway payload size limits.
    // ------------------------------------------------------------------
    const imageBucket = new s3.Bucket(this, 'FamilyChatImageBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [{
        allowedMethods: [s3.HttpMethods.PUT, s3.HttpMethods.GET],
        allowedOrigins: ['*'], // Note: In production, restrict to CloudFront domain
        allowedHeaders: ['*'],
      }],
      lifecycleRules: [{
        expiration: cdk.Duration.days(30), // Cost optimization and privacy
      }],
    });

    // Core WebSocket message handler
    const connectLambda = new lambda.Function(this, 'ConnectHandlerLambda', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'connect.lambda_handler',
      code: lambda.Code.fromAsset('lambda'),
      environment: {
        TABLE_NAME: connectionsTable.tableName,
        HISTORY_TABLE_NAME: chatHistoryTable.tableName,
        IMAGE_BUCKET_NAME: imageBucket.bucketName, // NEW: Inject bucket name
      }
    });

    connectionsTable.grantReadWriteData(connectLambda);
    chatHistoryTable.grantReadWriteData(connectLambda);
    // NEW: Grant Write permissions to generate presigned PUT URLs
    imageBucket.grantWrite(connectLambda); 

    // WebSocket API configuration
    const webSocketApi = new apigwv2.WebSocketApi(this, 'FamilyConnectApi', {
      apiName: 'FamilyConnectChat',
    });

    // Route integrations
    webSocketApi.addRoute('$disconnect', {
      integration: new WebSocketLambdaIntegration('DisconnectIntegration', connectLambda)
    });
    webSocketApi.addRoute('$default', {
      integration: new WebSocketLambdaIntegration('DefaultIntegration', connectLambda)
    });
    webSocketApi.addRoute('getHistory', {
      integration: new WebSocketLambdaIntegration('HistoryIntegration', connectLambda)
    });

    // API Gateway stage setup
    const apiStage = new apigwv2.WebSocketStage(this, 'DevStage', {
      webSocketApi,
      stageName: 'dev',
      autoDeploy: true,
    });

    // Allow Lambda to push messages back to connected clients via API Gateway
    connectLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['execute-api:ManageConnections'],
      resources: ['arn:aws:execute-api:*:*:*/*'],
    }));

    // Frontend hosting bucket
    const websiteBucket = new s3.Bucket(this, 'FamilyConnectWebsiteBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    const distribution = new cloudfront.Distribution(this, 'WebsiteDistribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(websiteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
      }
    });

    new s3deploy.BucketDeployment(this, 'DeployWebsite', {
      sources: [s3deploy.Source.asset('./frontend')],
      destinationBucket: websiteBucket,
      distribution,
      distributionPaths: ['/*'],
    });

    // CI/CD OIDC Provider for GitHub Actions
    const githubProvider = new iam.OpenIdConnectProvider(this, 'GitHubProvider', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
    });

    // Role for GitHub Actions to deploy CDK via OIDC
    const deployRole = new iam.Role(this, 'GitHubDeployRole', {
      assumedBy: new iam.FederatedPrincipal(
        githubProvider.openIdConnectProviderArn,
        {
          StringLike: {
            'token.actions.githubusercontent.com:sub': 'repo:atsuo-architect/family-connect:*',
          },
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
          },
        },
        'sts:AssumeRoleWithWebIdentity',
      ),
      description: 'Role for GitHub Actions to deploy CDK',
    });

    // ------------------------------------------------------------------
    // Cognito User Pool configuration
    // Rationale: Centralizes identity management and restricts chat access 
    // strictly to verified family members.
    // ------------------------------------------------------------------
    const userPool = new cognito.UserPool(this, 'FamilyConnectUserPool', {
      userPoolName: 'FamilyConnectUsers',
      selfSignUpEnabled: false, 
      signInAliases: { username: true, email: true }, 
      autoVerify: { email: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY, 
    });

    const userPoolClient = userPool.addClient('FamilyConnectAppClient', {
      authFlows: {
        userPassword: true, 
      },
    });

    // ------------------------------------------------------------------
    // Lambda Authorizer for WebSocket connection security
    // Rationale: Intercepts connection requests to validate Cognito tokens 
    // before allowing the WebSocket connection to be established.
    // ------------------------------------------------------------------
    const authLambda = new lambda.Function(this, 'WebSocketAuthHandler', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'auth.lambda_handler',
      code: lambda.Code.fromAsset('lambda'),
      environment: {
        USER_POOL_ID: userPool.userPoolId,
        APP_CLIENT_ID: userPoolClient.userPoolClientId,
      },
    });

    const authorizer = new apigwv2_authorizers.WebSocketLambdaAuthorizer('ChatAuthorizer', authLambda, {
      identitySource: ['route.request.querystring.token'], 
    });

    // ------------------------------------------------------------------
    // Apply Authorizer to the $connect route
    // Rationale: Secures the API endpoint at the infrastructure level.
    // ------------------------------------------------------------------
    webSocketApi.addRoute('$connect', {
      integration: new WebSocketLambdaIntegration('ConnectIntegration', connectLambda),
      authorizer: authorizer, 
    });


    // ==============================================================================
    // Asynchronous AI Handler Configuration & IAM Permissions
    // Rationale: Decouples AI inference from standard chat logic to maintain 
    // high performance and prevent connection timeouts during processing.
    // ==============================================================================
    const aiHandlerLambda = new lambda.Function(this, 'AiHandlerLambda', {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'ai_handler.lambda_handler',
      code: lambda.Code.fromAsset('lambda'),
      timeout: cdk.Duration.seconds(60), // Extended timeout allocated for Bedrock API inference
      environment: {
        HISTORY_TABLE_NAME: chatHistoryTable.tableName, 
        IMAGE_BUCKET_NAME: imageBucket.bucketName, // NEW: Inject bucket name
      }
    });

    // 1. Grant permission to invoke Amazon Bedrock foundational models
    aiHandlerLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: ['*'], 
    }));

    // 2. Grant write access to persist AI responses directly into the chat history
    chatHistoryTable.grantReadWriteData(aiHandlerLambda);

    // 3. Grant permission to broadcast the AI's response asynchronously via API Gateway
    aiHandlerLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ['execute-api:ManageConnections'],
      resources: [`arn:aws:execute-api:${this.region}:${this.account}:${webSocketApi.apiId}/*`],
    }));

    // 4. Grant the primary Connect Lambda permission to trigger the AI Handler asynchronously
    // Rationale: Enables event-driven delegation via environment variables, avoiding tight coupling.
    aiHandlerLambda.grantInvoke(connectLambda);
    connectLambda.addEnvironment('AI_LAMBDA_ARN', aiHandlerLambda.functionArn);
    
    // 5. NEW: Grant Read permissions to fetch uploaded images for multi-modal inference
    imageBucket.grantRead(aiHandlerLambda);
    // ==============================================================================


    // Outputs for Cognito configurations
    new cdk.CfnOutput(this, 'UserPoolId', { value: userPool.userPoolId });
    new cdk.CfnOutput(this, 'ClientId', { value: userPoolClient.userPoolClientId });

    // Grant deployment permissions
    deployRole.addManagedPolicy(iam.ManagedPolicy.fromAwsManagedPolicyName('AdministratorAccess'));

    // Output: Role ARN to be used in GitHub Actions Secrets
    new cdk.CfnOutput(this, 'DeployRoleArn', { value: deployRole.roleArn });

    // Output: Frontend website URL
    new cdk.CfnOutput(this, 'CloudFrontURL', {
      value: `https://${distribution.distributionDomainName}`, 
      description: 'The URL of the CloudFront distribution',
    });
    
    // Output: WebSocket endpoint URL
    new cdk.CfnOutput(this, 'WebSocketURL', {
      value: apiStage.url,
      description: 'The WSS URL to connect to the API Gateway',
    });
  }
}