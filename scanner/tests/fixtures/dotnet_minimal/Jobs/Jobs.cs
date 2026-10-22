using Confluent.Kafka;
using Hangfire;
using Microsoft.AspNetCore.SignalR;

namespace Minimal.Jobs;

public class Notifier
{
    private readonly IProducer<string, string> _producer;
    public Notifier(IProducer<string, string> producer) { _producer = producer; }
    public Task NotifyAsync(string message) => _producer.ProduceAsync("todo-events", new Message<string, string> { Value = message });
}

public class ChatHub : Hub
{
    public Task Send(string user, string message) => Clients.All.SendAsync("receive", user, message);
}

public class WeatherSync : BackgroundService
{
    private readonly IHttpClientFactory _factory;
    private readonly IConfiguration _configuration;
    public WeatherSync(IHttpClientFactory factory, IConfiguration configuration) { _factory = factory; _configuration = configuration; }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var client = _factory.CreateClient();
        await client.GetStringAsync(_configuration["Weather:Url"] + "/today", stoppingToken);
    }
}

public class Cleanup
{
    public void Run() { }
}
