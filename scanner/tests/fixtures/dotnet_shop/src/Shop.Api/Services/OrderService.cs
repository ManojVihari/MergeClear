using System.Text.Json;
using MassTransit;
using Microsoft.Extensions.Caching.Distributed;
using Microsoft.Extensions.Options;
using Shop.Api.Clients;
using Shop.Api.Messaging;
using Shop.Api.Models;
using Shop.Api.Options;
using Shop.Data.Entities;
using Shop.Data.Repositories;

namespace Shop.Api.Services;

public class NotFoundException : Exception
{
    public NotFoundException(string message) : base(message) { }
}

public class PaymentDeclinedException : Exception { }

public interface IOrderService
{
    Task<OrderDto> CreateAsync(CreateOrderRequest request);
    Task<OrderDto> GetAsync(int id);
    Task<PagedResult<OrderDto>> ListAsync(PageQuery page);
    Task CancelAsync(int id);
}

public class OrderService : IOrderService
{
    private readonly IRepository<Order> _orders;
    private readonly IRepository<AuditLog> _audit;
    private readonly IOrderRepository _orderRepository;
    private readonly IPaymentClient _payments;
    private readonly IPublishEndpoint _bus;
    private readonly IDistributedCache _cache;
    private readonly IHttpClientFactory _factory;
    private readonly InventoryOptions _inventory;

    public OrderService(IRepository<Order> orders, IRepository<AuditLog> audit, IOrderRepository orderRepository, IPaymentClient payments,
        IPublishEndpoint bus, IDistributedCache cache, IHttpClientFactory factory, IOptions<InventoryOptions> inventory)
    {
        _orders = orders; _audit = audit; _orderRepository = orderRepository; _payments = payments; _bus = bus; _cache = cache; _factory = factory;
        _inventory = inventory.Value;
    }

    public async Task<OrderDto> CreateAsync(CreateOrderRequest request)
    {
        var inventory = _factory.CreateClient("inventory");
        foreach (var line in request.Lines)
        {
            var stock = await inventory.GetFromJsonAsync<int>($"{_inventory.BaseUrl}/stock/{line.Sku}");
            if (stock < line.Quantity) throw new InvalidOperationException("Out of stock");
        }

        var order = new Order { CustomerEmail = request.CustomerEmail, Note = request.Note };
        await _orders.AddAsync(order);
        if (!await _payments.ChargeAsync(order.Id, 10m)) throw new PaymentDeclinedException();

        await _audit.AddAsync(new AuditLog { OrderId = order.Id, Action = "CREATE" });
        await _cache.SetStringAsync($"order:{order.Id}", JsonSerializer.Serialize(order));
        await _bus.Publish(new OrderCreated(order.Id, order.CustomerEmail));
        return new OrderDto { Id = order.Id, CustomerEmail = order.CustomerEmail };
    }

    public async Task<OrderDto> GetAsync(int id)
    {
        var cached = await _cache.GetStringAsync($"order:{id}");
        if (cached != null) return JsonSerializer.Deserialize<OrderDto>(cached)!;
        var order = await _orders.GetByIdAsync(id) ?? throw new NotFoundException($"Order {id} not found");
        return new OrderDto { Id = order.Id, CustomerEmail = order.CustomerEmail, Status = order.Status };
    }

    public async Task<PagedResult<OrderDto>> ListAsync(PageQuery page)
    {
        var all = await _orders.ListAsync();
        return new PagedResult<OrderDto> { Total = all.Count, Page = page.Page };
    }

    public async Task CancelAsync(int id)
    {
        var order = await _orders.GetByIdAsync(id);
        if (order is null) throw new NotFoundException("missing");
        await _orders.RemoveAsync(order);
    }
}
