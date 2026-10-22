using Hangfire;
using Minimal.Features;
using Minimal.Jobs;
using Microsoft.EntityFrameworkCore;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddDbContext<TodoDb>(o => o.UseSqlite(builder.Configuration.GetConnectionString("Default")));
builder.Services.AddHostedService<WeatherSync>();
builder.Services.AddScoped<Notifier>();
builder.Services.AddHttpClient();
builder.Services.AddSignalR();

var app = builder.Build();

app.UseExceptionHandler(handler => handler.Run(async context =>
{
    var feature = context.Features.Get<Microsoft.AspNetCore.Diagnostics.IExceptionHandlerFeature>();
    context.Response.StatusCode = feature?.Error switch
    {
        KeyNotFoundException => StatusCodes.Status404NotFound,
        ArgumentException => StatusCodes.Status400BadRequest,
        _ => StatusCodes.Status500InternalServerError,
    };
}));

var todos = app.MapGroup("/todos").WithTags("Todos");
TodoEndpoints.Map(todos);
app.MapHub<ChatHub>("/chat");
app.MapGet("/ping", () => "pong");
RecurringJob.AddOrUpdate<Cleanup>("cleanup", c => c.Run(), "0 3 * * *");

app.Run();
