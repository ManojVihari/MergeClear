"""
Knowledge about .NET libraries: what a type from a package is (EF Core context, Redis database, MassTransit bus, ...),
which methods read or write, which helpers answer with which HTTP status. Pure data plus tiny helpers.
"""
import re
from typing import Dict, Optional, Tuple

from mergeclear.core.http import STATUS_NAME  # noqa: F401  (used by the endpoint extractor through this module)
from mergeclear.core.tags import Tag

HTTP_ATTRIBUTES = {
    "HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT", "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}
MINIMAL_VERBS = {
    "MapGet": "GET", "MapPost": "POST", "MapPut": "PUT", "MapDelete": "DELETE", "MapPatch": "PATCH", "MapHead": "HEAD",
    "MapOptions": "OPTIONS", "MapMethods": None, "Map": None, "MapFallback": "GET",
}
CONTROLLER_BASES = {"ControllerBase", "Controller", "ApiController", "BaseController", "BaseApiController", "ODataController"}

# parameters ASP.NET supplies itself: they are not part of the HTTP contract
INJECTED_TYPES = {
    "HttpContext", "HttpRequest", "HttpResponse", "CancellationToken", "ClaimsPrincipal", "IFormCollection_", "ILogger", "IConfiguration",
    "IWebHostEnvironment", "IHostEnvironment", "IServiceProvider", "LinkGenerator", "IOptions", "IOptionsSnapshot", "IOptionsMonitor",
    "IMapper", "IMediator", "ISender", "IPublisher", "IValidator", "IAuthorizationService", "ProblemDetailsFactory", "TimeProvider",
    "IHttpContextAccessor", "IMemoryCache", "IDistributedCache", "IHttpClientFactory", "IFeatureManager", "IStringLocalizer", "ISystemClock",
    "IHostApplicationLifetime", "IBackgroundJobClient", "IPublishEndpoint", "ISendEndpointProvider", "IBus", "IServiceScopeFactory",
    "UserManager", "SignInManager", "RoleManager", "IUrlHelper", "IActionContextAccessor", "IDataProtectionProvider", "IAntiforgery",
    "Stream", "PipeReader", "IBodyControlFeature", "ModelStateDictionary", "BindingContext",
}
UPLOAD_TYPES = {"IFormFile", "IFormFileCollection", "List<IFormFile>"}
SIMPLE_TYPES = {
    "string", "int", "long", "short", "byte", "sbyte", "uint", "ulong", "ushort", "bool", "char", "float", "double", "decimal", "Guid",
    "DateTime", "DateTimeOffset", "DateOnly", "TimeOnly", "TimeSpan", "Uri", "object", "Version", "BigInteger", "Half", "IPAddress", "Ulid",
    "CultureInfo", "Type", "JsonElement", "JsonNode", "JsonObject", "JsonDocument", "IFormFile", "IFormFileCollection", "CancellationToken",
    "StringValues", "Stream", "byte[]", "dynamic",
}
COLLECTION_TYPES = {
    "List", "IList", "IEnumerable", "ICollection", "IReadOnlyList", "IReadOnlyCollection", "HashSet", "ISet", "IReadOnlySet", "Collection",
    "ReadOnlyCollection", "IAsyncEnumerable", "ImmutableArray", "ImmutableList", "Queue", "Stack", "LinkedList", "SortedSet", "Array",
    "ConcurrentBag", "BlockingCollection",
}
MAP_TYPES = {"Dictionary", "IDictionary", "IReadOnlyDictionary", "SortedDictionary", "ConcurrentDictionary", "ImmutableDictionary", "NameValueCollection"}
WRAPPER_TYPES = {
    "Task", "ValueTask", "ActionResult", "ObjectResult_", "Nullable", "Lazy", "Option", "JsonResult_", "Ok", "Created", "Accepted",
    "CreatedAtRoute", "AcceptedAtRoute", "Response", "ApiResponse", "Maybe", "ConfiguredTaskAwaitable",
}
RESULT_TYPES = {
    "IActionResult", "ActionResult", "IResult", "Results", "ObjectResult", "JsonResult", "ContentResult", "FileResult", "FileContentResult",
    "FileStreamResult", "PhysicalFileResult", "RedirectResult", "StatusCodeResult", "EmptyResult", "ViewResult", "PartialViewResult",
    "OkResult", "OkObjectResult", "NotFoundResult", "NoContentResult", "BadRequestResult", "TypedResult_", "HttpResults_", "IStatusCodeActionResult",
    "IConvertToActionResult",
}

# helper -> status code (ControllerBase methods, Results.* and TypedResults.*)
RESULT_HELPERS: Dict[str, Optional[int]] = {
    "Ok": 200, "Created": 201, "CreatedAtAction": 201, "CreatedAtRoute": 201, "Accepted": 202, "AcceptedAtAction": 202,
    "AcceptedAtRoute": 202, "NoContent": 204, "BadRequest": 400, "Unauthorized": 401, "Forbid": 403, "Forbidden": 403, "NotFound": 404,
    "Conflict": 409, "UnprocessableEntity": 422, "ValidationProblem": 400, "Challenge": 401, "Redirect": 302, "RedirectToAction": 302,
    "RedirectToRoute": 302, "LocalRedirect": 302, "RedirectPermanent": 301, "PermanentRedirect": 308, "TemporaryRedirect": 307, "File": 200,
    "Content": 200, "Json": 200, "Text": 200, "Stream": 200, "View": 200, "PartialView": 200, "Bytes": 200, "PhysicalFile": 200,
    "VirtualFile": 200, "Empty": 200, "StatusCode": None, "Problem": None, "SignIn": 200, "SignOut": 200, "Extensions_": None,
}
RESULT_HELPERS_BODY = {"Ok", "Created", "CreatedAtAction", "CreatedAtRoute", "Accepted", "AcceptedAtAction", "AcceptedAtRoute", "Json",
                       "BadRequest", "NotFound", "Conflict", "UnprocessableEntity"}
RESULT_RECEIVERS = {"Results", "TypedResults"}

# TypedResults generic result types: Results<Ok<T>, NotFound, BadRequest<ProblemDetails>>
TYPED_RESULT_STATUS = {
    "Ok": 200, "Created": 201, "CreatedAtRoute": 201, "Accepted": 202, "AcceptedAtRoute": 202, "NoContent": 204, "BadRequest": 400,
    "UnauthorizedHttpResult": 401, "Unauthorized": 401, "ForbidHttpResult": 403, "NotFound": 404, "Conflict": 409,
    "UnprocessableEntity": 422, "ValidationProblem": 400, "ProblemHttpResult": 500, "InternalServerError": 500, "RedirectHttpResult": 302,
    "FileContentHttpResult": 200, "FileStreamHttpResult": 200, "PushStreamHttpResult": 200, "ContentHttpResult": 200, "JsonHttpResult": 200,
    "Json": 200, "Content": 200, "StatusCodeHttpResult": None,
}

AUTH_SCHEMES = {
    "AddJwtBearer": "jwt-bearer", "AddCookie": "cookie", "AddOpenIdConnect": "openid-connect", "AddMicrosoftIdentityWebApi": "azure-ad",
    "AddMicrosoftIdentityWebApp": "azure-ad", "AddIdentityServerAuthentication": "identityserver", "AddOAuth": "oauth2",
    "AddGoogle": "google", "AddFacebook": "facebook", "AddMicrosoftAccount": "microsoft-account", "AddNegotiate": "windows",
    "AddCertificate": "client-certificate", "AddApiKey": "api-key", "AddBasic": "basic", "AddScheme": "custom", "AddIdentityCookies": "cookie",
    "AddBearerToken": "bearer-token", "AddTwitter": "twitter", "AddGitHub": "github", "AddAzureAD": "azure-ad", "AddOpenIdConnectAuthentication": "openid-connect",
}

# ============================================================================
# LIBRARY TYPES
# ============================================================================

# simple type name -> tag; an optional namespace prefix (`uses`) must be imported or the name is a project type
LIBRARY_TYPES: Dict[str, Tuple[Tag, Tuple[str, ...]]] = {
    # ---- ORM / SQL ----
    "DbContext": (Tag("db", "efcore", "context"), ()),
    "DbSet": (Tag("db", "efcore", "dbset"), ()),
    "DatabaseFacade": (Tag("db", "efcore", "database"), ()),
    "IQueryable": (Tag("db", "efcore", "query"), ()),
    "IDbConnection": (Tag("db", "sql", "connection"), ("System.Data",)),
    "DbConnection": (Tag("db", "sql", "connection"), ("System.Data",)),
    "IDbTransaction": (Tag("db", "sql", "transaction"), ("System.Data",)),
    "SqlConnection": (Tag("db", "sqlserver", "connection"), ()),
    "NpgsqlConnection": (Tag("db", "postgresql", "connection"), ()),
    "NpgsqlDataSource": (Tag("db", "postgresql", "connection"), ()),
    "MySqlConnection": (Tag("db", "mysql", "connection"), ()),
    "OracleConnection": (Tag("db", "oracle", "connection"), ()),
    "SqliteConnection": (Tag("db", "sqlite", "connection"), ()),
    "SqlCommand": (Tag("db", "sqlserver", "command"), ()),
    "NpgsqlCommand": (Tag("db", "postgresql", "command"), ()),
    "MySqlCommand": (Tag("db", "mysql", "command"), ()),
    "DbCommand": (Tag("db", "sql", "command"), ()),
    "IDbCommand": (Tag("db", "sql", "command"), ()),
    "IDbConnectionFactory": (Tag("db", "sql", "connection"), ()),
    "ISqlConnectionFactory": (Tag("db", "sql", "connection"), ()),
    # ---- document / key-value stores ----
    "IMongoCollection": (Tag("db", "mongodb", "collection"), ()),
    "IMongoDatabase": (Tag("db", "mongodb", "database"), ()),
    "IMongoClient": (Tag("db", "mongodb", "client"), ()),
    "MongoClient": (Tag("db", "mongodb", "client"), ()),
    "CosmosClient": (Tag("db", "cosmosdb", "client"), ()),
    "Container": (Tag("db", "cosmosdb", "container"), ("Microsoft.Azure.Cosmos",)),
    "IDynamoDBContext": (Tag("db", "dynamodb", "client"), ()),
    "IAmazonDynamoDB": (Tag("db", "dynamodb", "client"), ()),
    "TableClient": (Tag("db", "azure-table", "client"), ()),
    "IDocumentSession": (Tag("db", "marten", "session"), ()),
    "IDocumentStore": (Tag("db", "marten", "session"), ()),
    "IAsyncDocumentSession": (Tag("db", "ravendb", "session"), ()),
    "ISession": (Tag("db", "nhibernate", "session"), ("NHibernate",)),
    # ---- caches ----
    "IDistributedCache": (Tag("cache", "distributed-cache", "client"), ()),
    "IMemoryCache": (Tag("cache", "memory-cache", "client"), ()),
    "HybridCache": (Tag("cache", "hybrid-cache", "client"), ()),
    "IConnectionMultiplexer": (Tag("cache", "redis", "multiplexer"), ()),
    "ConnectionMultiplexer": (Tag("cache", "redis", "multiplexer"), ()),
    "IDatabase": (Tag("cache", "redis", "client"), ("StackExchange.Redis",)),
    "IDatabaseAsync": (Tag("cache", "redis", "client"), ("StackExchange.Redis",)),
    "ISubscriber": (Tag("cache", "redis", "subscriber"), ("StackExchange.Redis",)),
    "IOutputCacheStore": (Tag("cache", "output-cache", "client"), ()),
    "IEasyCachingProvider": (Tag("cache", "easycaching", "client"), ()),
    "IAppCache": (Tag("cache", "lazycache", "client"), ()),
    # ---- messaging ----
    "IPublishEndpoint": (Tag("broker", "masstransit", "bus"), ()),
    "ISendEndpointProvider": (Tag("broker", "masstransit", "bus"), ()),
    "ISendEndpoint": (Tag("broker", "masstransit", "bus"), ()),
    "IBusControl": (Tag("broker", "masstransit", "bus"), ()),
    "IRequestClient": (Tag("broker", "masstransit", "request-client"), ()),
    "IBus": (Tag("broker", "masstransit", "bus"), ()),
    "IMessageSession": (Tag("broker", "nservicebus", "bus"), ()),
    "IEndpointInstance": (Tag("broker", "nservicebus", "bus"), ()),
    "IMessageHandlerContext": (Tag("broker", "nservicebus", "bus"), ()),
    "IProducer": (Tag("broker", "kafka", "producer"), ("Confluent.Kafka",)),
    "IConsumer_kafka": (Tag("broker", "kafka", "consumer"), ()),
    "ProducerBuilder": (Tag("broker", "kafka", "producer"), ()),
    "ConsumerBuilder": (Tag("broker", "kafka", "consumer"), ()),
    "IModel": (Tag("broker", "rabbitmq", "channel"), ("RabbitMQ.Client",)),
    "IChannel": (Tag("broker", "rabbitmq", "channel"), ("RabbitMQ.Client",)),
    "IConnection": (Tag("broker", "rabbitmq", "connection"), ("RabbitMQ.Client",)),
    "ServiceBusClient": (Tag("broker", "azure-service-bus", "client"), ()),
    "ServiceBusSender": (Tag("broker", "azure-service-bus", "producer"), ()),
    "ServiceBusProcessor": (Tag("broker", "azure-service-bus", "consumer"), ()),
    "ServiceBusReceiver": (Tag("broker", "azure-service-bus", "consumer"), ()),
    "EventHubProducerClient": (Tag("broker", "azure-event-hubs", "producer"), ()),
    "EventProcessorClient": (Tag("broker", "azure-event-hubs", "consumer"), ()),
    "IAmazonSQS": (Tag("broker", "sqs", "producer"), ()),
    "IAmazonSimpleNotificationService": (Tag("broker", "sns", "producer"), ()),
    "IAmazonEventBridge": (Tag("broker", "eventbridge", "producer"), ()),
    "IAmazonKinesis": (Tag("broker", "kinesis", "producer"), ()),
    "PublisherClient": (Tag("broker", "pubsub", "producer"), ()),
    "SubscriberClient": (Tag("broker", "pubsub", "consumer"), ()),
    "INatsConnection": (Tag("broker", "nats", "producer"), ()),
    "DaprClient": (Tag("broker", "dapr", "producer"), ()),
    "IBackgroundJobClient": (Tag("broker", "hangfire", "queue"), ()),
    "IRecurringJobManager": (Tag("broker", "hangfire", "scheduler"), ()),
    "ISchedulerFactory": (Tag("broker", "quartz", "scheduler"), ()),
    "IScheduler": (Tag("broker", "quartz", "scheduler"), ()),
    "IMediator": (Tag("framework", "mediatr", "mediator"), ()),
    "ISender": (Tag("framework", "mediatr", "mediator"), ()),
    "IPublisher": (Tag("framework", "mediatr", "mediator"), ()),
    "IMessageBus": (Tag("framework", "wolverine", "mediator"), ()),
    "IDispatcher": (Tag("framework", "dispatcher", "mediator"), ()),
    # ---- HTTP ----
    "HttpClient": (Tag("http", "httpclient", "client"), ()),
    "IHttpClientFactory": (Tag("http", "httpclient", "factory"), ()),
    "RestClient": (Tag("http", "restsharp", "client"), ()),
    "IRestClient": (Tag("http", "restsharp", "client"), ()),
    "WebClient": (Tag("http", "webclient", "client"), ()),
    "IFlurlClient": (Tag("http", "flurl", "client"), ()),
    # ---- storage / email / search ----
    "IAmazonS3": (Tag("storage", "s3", "client"), ()),
    "BlobServiceClient": (Tag("storage", "azure-blob", "client"), ()),
    "BlobContainerClient": (Tag("storage", "azure-blob", "client"), ()),
    "BlobClient": (Tag("storage", "azure-blob", "client"), ()),
    "StorageClient": (Tag("storage", "gcs", "client"), ()),
    "IMinioClient": (Tag("storage", "minio", "client"), ()),
    "SmtpClient": (Tag("email", "smtp", "client"), ()),
    "ISmtpClient": (Tag("email", "smtp", "client"), ()),
    "ISendGridClient": (Tag("email", "sendgrid", "client"), ()),
    "IFluentEmail": (Tag("email", "fluentemail", "client"), ()),
    "IEmailSender": (Tag("email", "email-sender", "client"), ("Microsoft.AspNetCore.Identity.UI.Services", "Microsoft.AspNetCore.Identity")),
    "IAmazonSimpleEmailService": (Tag("email", "ses", "client"), ()),
    "ElasticsearchClient": (Tag("search", "elasticsearch", "client"), ()),
    "ElasticClient": (Tag("search", "elasticsearch", "client"), ()),
    "IElasticClient": (Tag("search", "elasticsearch", "client"), ()),
    "OpenSearchClient": (Tag("search", "opensearch", "client"), ()),
    "IOpenSearchClient": (Tag("search", "opensearch", "client"), ()),
    "SearchClient": (Tag("search", "azure-search", "client"), ()),
    "MeilisearchClient": (Tag("search", "meilisearch", "client"), ()),
    "ISearchClient": (Tag("search", "algolia", "client"), ()),
}

KAFKA_CONSUMER = Tag("broker", "kafka", "consumer")

# ============================================================================
# METHODS
# ============================================================================

EF_READ = {
    "ToList", "ToListAsync", "ToArray", "ToArrayAsync", "FirstOrDefault", "FirstOrDefaultAsync", "First", "FirstAsync", "Single",
    "SingleAsync", "SingleOrDefault", "SingleOrDefaultAsync", "Find", "FindAsync", "Any", "AnyAsync", "All", "AllAsync", "Count", "CountAsync",
    "LongCount", "LongCountAsync", "Sum", "SumAsync", "Average", "AverageAsync", "Min", "MinAsync", "Max", "MaxAsync", "Contains", "Last",
    "LastAsync", "LastOrDefault", "LastOrDefaultAsync", "ElementAt", "AsEnumerable", "AsAsyncEnumerable", "ToDictionaryAsync", "ToDictionary",
    "ToHashSetAsync", "Where", "Select", "Include", "ThenInclude", "AsNoTracking", "AsNoTrackingWithIdentityResolution", "OrderBy",
    "OrderByDescending", "ThenBy", "GroupBy", "Skip", "Take", "Distinct", "AsQueryable", "AsSplitQuery", "Load", "LoadAsync", "ToPagedList",
    "ToPagedListAsync", "ProjectTo", "SelectMany", "Join", "Reverse", "Local", "Entries",
}
EF_WRITE = {"Add", "AddAsync", "AddRange", "AddRangeAsync", "Update", "UpdateRange", "Attach", "AttachRange", "ExecuteUpdate", "ExecuteUpdateAsync",
            "BulkInsert", "BulkInsertAsync", "BulkUpdate", "BulkUpdateAsync", "BulkMerge", "BulkMergeAsync", "Upsert"}
EF_DELETE = {"Remove", "RemoveRange", "ExecuteDelete", "ExecuteDeleteAsync", "BulkDelete", "BulkDeleteAsync"}
EF_COMMIT = {"SaveChanges", "SaveChangesAsync", "CommitAsync", "Commit", "CompleteAsync", "Complete", "SaveAsync", "Save"}
EF_TX = {"BeginTransaction", "BeginTransactionAsync", "UseTransaction", "Rollback", "RollbackAsync"}
EF_RAW_READ = {"FromSqlRaw", "FromSqlInterpolated", "FromSql", "SqlQuery", "SqlQueryRaw"}
EF_RAW_WRITE = {"ExecuteSqlRaw", "ExecuteSqlRawAsync", "ExecuteSqlInterpolated", "ExecuteSqlInterpolatedAsync", "ExecuteSql", "ExecuteSqlAsync"}
DAPPER_METHODS = {
    "Query", "QueryAsync", "QueryFirst", "QueryFirstAsync", "QueryFirstOrDefault", "QueryFirstOrDefaultAsync", "QuerySingle", "QuerySingleAsync",
    "QuerySingleOrDefault", "QuerySingleOrDefaultAsync", "QueryMultiple", "QueryMultipleAsync", "Execute", "ExecuteAsync", "ExecuteScalar",
    "ExecuteScalarAsync", "ExecuteReader", "ExecuteReaderAsync", "ExecuteScalar_", "Insert", "InsertAsync", "Update", "UpdateAsync", "Delete",
    "DeleteAsync", "Get", "GetAsync", "GetAll", "GetAllAsync",
}
COMMAND_EXECUTE = {"ExecuteReader", "ExecuteReaderAsync", "ExecuteNonQuery", "ExecuteNonQueryAsync", "ExecuteScalar", "ExecuteScalarAsync"}
MONGO_READ = {"Find", "FindAsync", "FindSync", "Aggregate", "AggregateAsync", "CountDocuments", "CountDocumentsAsync", "EstimatedDocumentCount",
              "Distinct", "DistinctAsync", "AsQueryable", "Watch"}
MONGO_WRITE = {"InsertOne", "InsertOneAsync", "InsertMany", "InsertManyAsync", "ReplaceOne", "ReplaceOneAsync", "UpdateOne", "UpdateOneAsync",
               "UpdateMany", "UpdateManyAsync", "FindOneAndUpdate", "FindOneAndUpdateAsync", "FindOneAndReplace", "FindOneAndReplaceAsync",
               "BulkWrite", "BulkWriteAsync"}
MONGO_DELETE = {"DeleteOne", "DeleteOneAsync", "DeleteMany", "DeleteManyAsync", "FindOneAndDelete", "FindOneAndDeleteAsync", "DropCollection",
                "DropCollectionAsync"}
COSMOS_READ = {"ReadItemAsync", "ReadItemStreamAsync", "GetItemLinqQueryable", "GetItemQueryIterator", "ReadManyItemsAsync"}
COSMOS_WRITE = {"CreateItemAsync", "UpsertItemAsync", "ReplaceItemAsync", "PatchItemAsync", "CreateItemStreamAsync", "UpsertItemStreamAsync"}
COSMOS_DELETE = {"DeleteItemAsync", "DeleteItemStreamAsync"}

DISTRIBUTED_READ = {"Get", "GetAsync", "GetString", "GetStringAsync", "TryGetValue", "GetObject", "GetObjectAsync", "Exists", "ExistsAsync"}
DISTRIBUTED_WRITE = {"Set", "SetAsync", "SetString", "SetStringAsync", "SetObject", "SetObjectAsync", "Refresh", "RefreshAsync", "CreateEntry", "Add", "AddAsync"}
DISTRIBUTED_DELETE = {"Remove", "RemoveAsync", "RemoveByTagAsync", "Clear"}
CACHE_THROUGH = {"GetOrCreate", "GetOrCreateAsync", "GetOrAdd", "GetOrAddAsync", "GetOrSetAsync", "GetOrSet"}
REDIS_READ = {
    "StringGet", "StringGetAsync", "HashGet", "HashGetAsync", "HashGetAll", "HashGetAllAsync", "HashExists", "HashExistsAsync", "HashValues",
    "HashKeys", "ListRange", "ListRangeAsync", "ListGetByIndex", "ListLength", "SetMembers", "SetMembersAsync", "SetContains", "SetContainsAsync",
    "SetLength", "SortedSetRangeByRank", "SortedSetRangeByScore", "SortedSetScore", "SortedSetLength", "KeyExists", "KeyExistsAsync", "KeyTimeToLive",
    "StringLength", "StreamRead", "StreamRange", "KeyType", "ScriptEvaluate",
}
REDIS_WRITE = {
    "StringSet", "StringSetAsync", "StringAppend", "StringIncrement", "StringIncrementAsync", "StringDecrement", "HashSet", "HashSetAsync",
    "HashIncrement", "ListLeftPush", "ListLeftPushAsync", "ListRightPush", "ListRightPushAsync", "ListSetByIndex", "ListTrim", "SetAdd", "SetAddAsync",
    "SortedSetAdd", "SortedSetAddAsync", "SortedSetIncrement", "KeyExpire", "KeyExpireAsync", "KeyPersist", "KeyRename", "StringGetSet",
    "StringGetSetExpiry", "StreamAdd", "ListLeftPop", "ListRightPop", "SetPop", "LockTake", "LockTakeAsync",
}
REDIS_DELETE = {"KeyDelete", "KeyDeleteAsync", "HashDelete", "HashDeleteAsync", "ListRemove", "ListRemoveAsync", "SetRemove", "SetRemoveAsync",
                "SortedSetRemove", "SortedSetRemoveAsync", "StreamDelete", "LockRelease", "LockReleaseAsync"}
REDIS_PUBLISH = {"Publish", "PublishAsync"}

PUBLISH_METHODS = {
    "masstransit": {"Publish", "Send", "GetSendEndpoint", "GetResponse", "SendBatch", "PublishBatch", "Execute_"},
    "nservicebus": {"Send", "Publish", "SendLocal", "Reply"},
    "kafka": {"Produce", "ProduceAsync"},
    "rabbitmq": {"BasicPublish", "BasicPublishAsync"},
    "azure-service-bus": {"SendMessageAsync", "SendMessagesAsync", "ScheduleMessageAsync", "SendAsync"},
    "azure-event-hubs": {"SendAsync", "CreateBatchAsync"},
    "sqs": {"SendMessageAsync", "SendMessageBatchAsync"},
    "sns": {"PublishAsync", "PublishBatchAsync"},
    "eventbridge": {"PutEventsAsync"},
    "kinesis": {"PutRecordAsync", "PutRecordsAsync"},
    "pubsub": {"PublishAsync"},
    "nats": {"PublishAsync"},
    "dapr": {"PublishEventAsync", "InvokeBindingAsync"},
    "hangfire": {"Enqueue", "Schedule", "ContinueWith"},
}
HTTP_CLIENT_METHODS = {
    "GetAsync", "GetStringAsync", "GetStreamAsync", "GetByteArrayAsync", "GetFromJsonAsync", "GetFromJson", "PostAsync", "PostAsJsonAsync",
    "PostAsXmlAsync", "PutAsync", "PutAsJsonAsync", "PatchAsync", "PatchAsJsonAsync", "DeleteAsync", "DeleteFromJsonAsync", "HeadAsync",
    "SendAsync", "Send", "GetAsyncEnumerable", "GetFromJsonAsAsyncEnumerable", "ExecuteAsync", "ExecuteGetAsync", "ExecutePostAsync",
    "DownloadString", "DownloadStringAsync", "UploadString", "DownloadData", "DownloadDataAsync", "UploadStringAsync",
}
HTTP_METHOD_OF = {
    "get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "head": "HEAD", "options": "OPTIONS",
}
REFIT_ATTRIBUTES = {"Get": "GET", "Post": "POST", "Put": "PUT", "Delete": "DELETE", "Patch": "PATCH", "Head": "HEAD", "Options": "OPTIONS"}
STORAGE_READ = {"GetObjectAsync", "GetObjectMetadataAsync", "GetPreSignedURL", "ListObjectsV2Async", "ListObjectsAsync", "DownloadAsync", "DownloadToAsync",
                "DownloadContentAsync", "OpenReadAsync", "DownloadStreamingAsync", "GetBlobClient", "ExistsAsync", "DownloadObjectAsync", "GetBlobsAsync"}
STORAGE_WRITE = {"PutObjectAsync", "UploadAsync", "UploadBlobAsync", "UploadObjectAsync", "CopyObjectAsync", "UploadFileAsync", "PutObject",
                 "InitiateMultipartUploadAsync", "UploadFromStreamAsync", "CreateIfNotExistsAsync", "StartCopyFromUriAsync"}
STORAGE_DELETE = {"DeleteObjectAsync", "DeleteObjectsAsync", "DeleteAsync", "DeleteBlobAsync", "DeleteIfExistsAsync", "DeleteBlobIfExistsAsync", "DeleteObject"}
EMAIL_METHODS = {"Send", "SendAsync", "SendMailAsync", "SendEmailAsync", "SendEmail", "SendMail", "SendBulkEmailAsync"}
SEARCH_READ = {"Search", "SearchAsync", "Get", "GetAsync", "Count", "CountAsync", "Scroll", "ScrollAsync", "MultiSearch", "Suggest", "Exists", "ExistsAsync"}
SEARCH_WRITE = {"Index", "IndexAsync", "IndexDocument", "IndexDocumentAsync", "IndexMany", "IndexManyAsync", "Bulk", "BulkAsync", "Update", "UpdateAsync",
                "UpsertAsync", "UploadDocumentsAsync", "MergeOrUploadDocumentsAsync", "AddDocumentsAsync", "SaveObjectAsync", "Create", "CreateAsync"}
SEARCH_DELETE = {"Delete", "DeleteAsync", "DeleteByQuery", "DeleteByQueryAsync", "DeleteDocumentsAsync", "DeleteIndex", "DeleteIndexAsync"}

RESILIENCE_ATTRIBUTES = {
    "EnableRateLimiting": "rate-limit", "DisableRateLimiting": "rate-limit-off", "RequestTimeout": "timeout", "OutputCache": "output-cache",
    "ResponseCache": "response-cache", "RateLimit": "rate-limit", "Retry": "retry", "CircuitBreaker": "circuit-breaker", "Timeout": "timeout",
    "AutomaticRetry": "retry", "DisableConcurrentExecution": "concurrency-limit", "EnableCors": "cors",
}
RESILIENCE_CALLS = {
    "WaitAndRetryAsync": "retry", "RetryAsync": "retry", "WaitAndRetry": "retry", "Retry": "retry", "CircuitBreakerAsync": "circuit-breaker",
    "AdvancedCircuitBreakerAsync": "circuit-breaker", "CircuitBreaker": "circuit-breaker", "TimeoutAsync": "timeout", "BulkheadAsync": "bulkhead",
    "AddRetry": "retry", "AddCircuitBreaker": "circuit-breaker", "AddTimeout": "timeout", "AddPolicyHandler": "polly", "AddResilienceHandler": "polly",
    "AddStandardResilienceHandler": "polly", "AddTransientHttpErrorPolicy": "polly", "ExecuteAsync_": "polly",
}
ASYNC_LAUNCHERS = {"Run": "task", "Start": "task", "StartNew": "task", "QueueUserWorkItem": "thread-pool", "QueueBackgroundWorkItem": "background-queue",
                   "QueueBackgroundWorkItemAsync": "background-queue", "WriteAsync": None, "TryWrite": None}

# ============================================================================
# PACKAGES (module summary)
# ============================================================================

PACKAGES: Dict[str, Tuple[str, str]] = {
    "Microsoft.AspNetCore.App": ("web", "ASP.NET Core"), "Microsoft.AspNetCore.Mvc.NewtonsoftJson": ("web", "Newtonsoft JSON"),
    "Microsoft.AspNetCore.OpenApi": ("web", "OpenAPI"), "Swashbuckle.AspNetCore": ("web", "Swagger (Swashbuckle)"),
    "NSwag.AspNetCore": ("web", "Swagger (NSwag)"), "Asp.Versioning.Http": ("web", "API versioning"), "Asp.Versioning.Mvc": ("web", "API versioning"),
    "Microsoft.AspNetCore.Mvc.Versioning": ("web", "API versioning"), "Carter": ("web", "Carter"), "FastEndpoints": ("web", "FastEndpoints"),
    "Ardalis.ApiEndpoints": ("web", "ApiEndpoints"), "Microsoft.AspNetCore.SignalR": ("web", "SignalR"), "Grpc.AspNetCore": ("web", "gRPC"),
    "HotChocolate.AspNetCore": ("web", "GraphQL (HotChocolate)"), "Microsoft.AspNetCore.OData": ("web", "OData"),
    "Microsoft.EntityFrameworkCore": ("database", "EF Core"), "Microsoft.EntityFrameworkCore.SqlServer": ("database", "SQL Server"),
    "Npgsql.EntityFrameworkCore.PostgreSQL": ("database", "PostgreSQL"), "Npgsql": ("database", "PostgreSQL"),
    "Pomelo.EntityFrameworkCore.MySql": ("database", "MySQL"), "MySql.Data": ("database", "MySQL"), "MySqlConnector": ("database", "MySQL"),
    "Microsoft.EntityFrameworkCore.Sqlite": ("database", "SQLite"), "Microsoft.Data.Sqlite": ("database", "SQLite"),
    "Microsoft.Data.SqlClient": ("database", "SQL Server"), "System.Data.SqlClient": ("database", "SQL Server"),
    "Oracle.EntityFrameworkCore": ("database", "Oracle"), "Oracle.ManagedDataAccess.Core": ("database", "Oracle"),
    "Microsoft.EntityFrameworkCore.InMemory": ("database", "EF Core InMemory"), "Microsoft.EntityFrameworkCore.Cosmos": ("database", "Cosmos DB"),
    "Dapper": ("database", "Dapper"), "Dapper.Contrib": ("database", "Dapper"), "linq2db": ("database", "linq2db"), "NHibernate": ("database", "NHibernate"),
    "MongoDB.Driver": ("database", "MongoDB"), "Microsoft.Azure.Cosmos": ("database", "Cosmos DB"), "AWSSDK.DynamoDBv2": ("database", "DynamoDB"),
    "Marten": ("database", "Marten"), "RavenDB.Client": ("database", "RavenDB"), "FluentMigrator": ("database", "FluentMigrator"),
    "EFCore.BulkExtensions": ("database", "EF Core bulk"), "Z.EntityFramework.Extensions": ("database", "EF Core bulk"),
    "StackExchange.Redis": ("cache", "Redis"), "Microsoft.Extensions.Caching.StackExchangeRedis": ("cache", "Redis"),
    "Microsoft.Extensions.Caching.Memory": ("cache", "Memory cache"), "Microsoft.Extensions.Caching.Hybrid": ("cache", "HybridCache"),
    "EasyCaching.Core": ("cache", "EasyCaching"), "LazyCache": ("cache", "LazyCache"), "ZiggyCreatures.FusionCache": ("cache", "FusionCache"),
    "MassTransit": ("messaging", "MassTransit"), "MassTransit.RabbitMQ": ("messaging", "RabbitMQ"), "MassTransit.Azure.ServiceBus.Core": ("messaging", "Azure Service Bus"),
    "MassTransit.Kafka": ("messaging", "Kafka"), "MassTransit.AmazonSQS": ("messaging", "SQS"), "NServiceBus": ("messaging", "NServiceBus"),
    "Rebus": ("messaging", "Rebus"), "Confluent.Kafka": ("messaging", "Kafka"), "RabbitMQ.Client": ("messaging", "RabbitMQ"),
    "Azure.Messaging.ServiceBus": ("messaging", "Azure Service Bus"), "Azure.Messaging.EventHubs": ("messaging", "Azure Event Hubs"),
    "AWSSDK.SQS": ("messaging", "SQS"), "AWSSDK.SimpleNotificationService": ("messaging", "SNS"), "Google.Cloud.PubSub.V1": ("messaging", "Google Pub/Sub"),
    "NATS.Client": ("messaging", "NATS"), "NATS.Net": ("messaging", "NATS"), "Dapr.AspNetCore": ("messaging", "Dapr"), "Dapr.Client": ("messaging", "Dapr"),
    "Hangfire": ("scheduling", "Hangfire"), "Hangfire.AspNetCore": ("scheduling", "Hangfire"), "Hangfire.Core": ("scheduling", "Hangfire"),
    "Quartz": ("scheduling", "Quartz.NET"), "Quartz.Extensions.Hosting": ("scheduling", "Quartz.NET"), "Coravel": ("scheduling", "Coravel"),
    "MediatR": ("cqrs", "MediatR"), "MediatR.Extensions.Microsoft.DependencyInjection": ("cqrs", "MediatR"), "Wolverine": ("cqrs", "Wolverine"),
    "AutoMapper": ("mapping", "AutoMapper"), "Mapster": ("mapping", "Mapster"), "FluentValidation": ("validation", "FluentValidation"),
    "FluentValidation.AspNetCore": ("validation", "FluentValidation"), "FluentValidation.DependencyInjectionExtensions": ("validation", "FluentValidation"),
    "Microsoft.AspNetCore.Authentication.JwtBearer": ("security", "JWT Bearer"), "Microsoft.AspNetCore.Authentication.OpenIdConnect": ("security", "OpenID Connect"),
    "Microsoft.Identity.Web": ("security", "Microsoft Identity (Azure AD)"), "Microsoft.AspNetCore.Identity.EntityFrameworkCore": ("security", "ASP.NET Identity"),
    "Duende.IdentityServer": ("security", "IdentityServer"), "IdentityServer4": ("security", "IdentityServer"), "OpenIddict.AspNetCore": ("security", "OpenIddict"),
    "Microsoft.AspNetCore.Authentication.Google": ("security", "Google sign-in"), "Microsoft.AspNetCore.Authentication.MicrosoftAccount": ("security", "Microsoft sign-in"),
    "AspNetCoreRateLimit": ("security", "Rate limiting"), "NWebsec.AspNetCore.Middleware": ("security", "NWebsec"), "BCrypt.Net-Next": ("security", "BCrypt"),
    "Polly": ("resilience", "Polly"), "Microsoft.Extensions.Http.Polly": ("resilience", "Polly"), "Microsoft.Extensions.Http.Resilience": ("resilience", "HTTP resilience"),
    "Refit": ("http-client", "Refit"), "Refit.HttpClientFactory": ("http-client", "Refit"), "RestSharp": ("http-client", "RestSharp"), "Flurl.Http": ("http-client", "Flurl"),
    "AWSSDK.S3": ("storage", "S3"), "Azure.Storage.Blobs": ("storage", "Azure Blob Storage"), "Google.Cloud.Storage.V1": ("storage", "Google Cloud Storage"),
    "Minio": ("storage", "MinIO"), "MailKit": ("email", "MailKit"), "SendGrid": ("email", "SendGrid"), "FluentEmail.Core": ("email", "FluentEmail"),
    "NEST": ("search", "Elasticsearch"), "Elastic.Clients.Elasticsearch": ("search", "Elasticsearch"), "OpenSearch.Client": ("search", "OpenSearch"),
    "Azure.Search.Documents": ("search", "Azure AI Search"), "Meilisearch": ("search", "Meilisearch"),
    "Serilog": ("observability", "Serilog"), "Serilog.AspNetCore": ("observability", "Serilog"), "NLog": ("observability", "NLog"),
    "OpenTelemetry": ("observability", "OpenTelemetry"), "OpenTelemetry.Extensions.Hosting": ("observability", "OpenTelemetry"),
    "prometheus-net.AspNetCore": ("observability", "Prometheus"), "Sentry.AspNetCore": ("observability", "Sentry"),
    "Microsoft.ApplicationInsights.AspNetCore": ("observability", "Application Insights"), "AspNetCore.HealthChecks.UI": ("observability", "Health checks"),
    "Elastic.Apm.NetCoreAll": ("observability", "Elastic APM"), "Datadog.Trace": ("observability", "Datadog"),
    "Microsoft.NET.Test.Sdk": ("test", "Test SDK"), "xunit": ("test", "xUnit"), "NUnit": ("test", "NUnit"), "MSTest.TestFramework": ("test", "MSTest"),
    "Moq": ("test", "Moq"), "NSubstitute": ("test", "NSubstitute"), "FluentAssertions": ("test", "FluentAssertions"), "Bogus": ("test", "Bogus"),
    "Testcontainers": ("test", "Testcontainers"), "Microsoft.AspNetCore.Mvc.Testing": ("test", "WebApplicationFactory"), "coverlet.collector": ("test", "coverlet"),
}
IGNORED_CATEGORIES = {"test"}
_NORMALIZED = {k.lower(): v for k, v in PACKAGES.items()}


def package_info(name: str) -> Optional[Tuple[str, str]]:
    key = name.lower()
    if key in _NORMALIZED:
        return _NORMALIZED[key]
    for prefix, value in (("microsoft.entityframeworkcore.", ("database", "EF Core")), ("masstransit.", ("messaging", "MassTransit")),
                          ("opentelemetry.", ("observability", "OpenTelemetry")), ("serilog.", ("observability", "Serilog")),
                          ("awssdk.", ("cloud", "AWS SDK")), ("azure.", ("cloud", "Azure SDK")), ("microsoft.aspnetcore.authentication.", ("security", "Authentication")),
                          ("hangfire.", ("scheduling", "Hangfire")), ("swashbuckle.", ("web", "Swagger (Swashbuckle)")),
                          ("xunit.", ("test", "xUnit")), ("nunit", ("test", "NUnit")), ("mstest", ("test", "MSTest"))):
        if key.startswith(prefix):
            return value
    return None


# connection-string / setting keys -> infrastructure kind
SECRET_KEY = re.compile(r"(secret|password|passwd|pwd|token|api_?key|private|credential|signing|connectionstring|accountkey|sharedaccess)", re.I)


def is_secret_name(name: str) -> bool:
    return bool(SECRET_KEY.search(name))
