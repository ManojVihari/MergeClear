using MassTransit;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.EntityFrameworkCore;
using Shop.Api.Clients;
using Shop.Api.Endpoints;
using Shop.Api.Messaging;
using Shop.Api.Middleware;
using Shop.Api.Options;
using Shop.Api.Services;
using Shop.Data;
using Shop.Data.Repositories;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddControllers();
builder.Services.AddDbContext<ShopDbContext>(o => o.UseNpgsql(builder.Configuration.GetConnectionString("Shop")));
builder.Services.AddScoped<IOrderService, OrderService>();
builder.Services.AddScoped<IOrderRepository, OrderRepository>();
builder.Services.AddScoped(typeof(IRepository<>), typeof(EfRepository<>));
builder.Services.AddHttpClient<IPaymentClient, PaymentClient>(c => c.BaseAddress = new Uri(builder.Configuration["Payments:BaseUrl"]!));
builder.Services.AddHttpClient("inventory", c => c.BaseAddress = new Uri(builder.Configuration["Inventory:BaseUrl"]!));
builder.Services.Configure<InventoryOptions>(builder.Configuration.GetSection("Inventory"));
builder.Services.AddStackExchangeRedisCache(o => o.Configuration = builder.Configuration.GetConnectionString("Redis"));
builder.Services.AddMediatR(cfg => cfg.RegisterServicesFromAssembly(typeof(Program).Assembly));
builder.Services.AddMassTransit(x =>
{
    x.AddConsumer<OrderPaidConsumer>();
    x.UsingRabbitMq((ctx, cfg) =>
    {
        cfg.Host("rabbit.internal");
        cfg.ReceiveEndpoint("order-paid", e => e.ConfigureConsumer<OrderPaidConsumer>(ctx));
    });
});
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme).AddJwtBearer();
builder.Services.AddAuthorizationBuilder().AddPolicy("AdminOnly", p => p.RequireRole("Admin"));

var app = builder.Build();

app.UseMiddleware<ExceptionMiddleware>();
app.UseAuthentication();
app.UseAuthorization();

app.MapControllers();
app.MapGroup("/api/v2").RequireAuthorization().MapOrderEndpoints();
app.MapGet("/health", () => Results.Ok(new { status = "ok" })).AllowAnonymous();

app.Run();
