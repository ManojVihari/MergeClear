using MassTransit;
using MediatR;
using Shop.Data;

namespace Shop.Api.Messaging;

public record OrderCreated(int OrderId, string Email);
public record OrderPaid(int OrderId);

public class OrderPaidConsumer : IConsumer<OrderPaid>
{
    private readonly ShopDbContext _db;
    public OrderPaidConsumer(ShopDbContext db) { _db = db; }

    public async Task Consume(ConsumeContext<OrderPaid> context)
    {
        var order = await _db.Orders.FindAsync(context.Message.OrderId);
        if (order != null) { order.Status = Shop.Data.Entities.OrderStatus.Paid; await _db.SaveChangesAsync(); }
    }
}

public record PayOrderCommand(int OrderId) : IRequest<bool>;

public class PayOrderHandler : IRequestHandler<PayOrderCommand, bool>
{
    private readonly IPublishEndpoint _bus;
    public PayOrderHandler(IPublishEndpoint bus) { _bus = bus; }

    public async Task<bool> Handle(PayOrderCommand request, CancellationToken cancellationToken)
    {
        await _bus.Publish(new OrderPaid(request.OrderId), cancellationToken);
        return true;
    }
}
