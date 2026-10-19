using Microsoft.AspNetCore.Http.HttpResults;
using Shop.Api.Models;
using Shop.Api.Services;

namespace Shop.Api.Endpoints;

public static class OrderEndpoints
{
    public static RouteGroupBuilder MapOrderEndpoints(this IEndpointRouteBuilder app)
    {
        var group = app.MapGroup("/orders").WithTags("OrdersV2");

        group.MapGet("/{id:int}", async (int id, IOrderService service) =>
        {
            var order = await service.GetAsync(id);
            return Results.Ok(order);
        })
        .WithName("GetOrderV2")
        .Produces<OrderDto>(200)
        .ProducesProblem(404);

        group.MapPost("/", CreateOrder).AddEndpointFilter<ValidationFilter>();

        group.MapDelete("/{id:int}", async (int id, IOrderService service) =>
        {
            await service.CancelAsync(id);
            return TypedResults.NoContent();
        }).RequireAuthorization("AdminOnly");

        return group;
    }

    private static async Task<Results<Created<OrderDto>, BadRequest>> CreateOrder(CreateOrderRequest request, IOrderService service)
    {
        var order = await service.CreateAsync(request);
        return TypedResults.Created($"/orders/{order.Id}", order);
    }
}

public class ValidationFilter : IEndpointFilter
{
    public async ValueTask<object?> InvokeAsync(EndpointFilterInvocationContext context, EndpointFilterDelegate next) => await next(context);
}
