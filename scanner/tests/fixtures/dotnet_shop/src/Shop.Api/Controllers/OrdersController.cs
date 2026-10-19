using MediatR;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Shop.Api.Messaging;
using Shop.Api.Models;
using Shop.Api.Services;

namespace Shop.Api.Controllers;

/// <summary>Orders of the shop.</summary>
[ApiController]
[Route("api/[controller]")]
[Authorize]
public class OrdersController(IOrderService orders, IMediator mediator) : ControllerBase
{
    /// <summary>Places a new order.</summary>
    [HttpPost]
    [ProducesResponseType(typeof(OrderDto), StatusCodes.Status201Created)]
    [ProducesResponseType(StatusCodes.Status402PaymentRequired)]
    public async Task<IActionResult> Create([FromBody] CreateOrderRequest request, [FromHeader(Name = "X-Request-Id")] string requestId)
    {
        var order = await orders.CreateAsync(request);
        return CreatedAtAction(nameof(Get), new { id = order.Id }, order);
    }

    [HttpGet("{id:int}")]
    public async Task<ActionResult<OrderDto>> Get(int id)
    {
        var order = await orders.GetAsync(id);
        if (order is null) return NotFound();
        return Ok(order);
    }

    [HttpGet]
    [AllowAnonymous]
    public async Task<ActionResult<PagedResult<OrderDto>>> List([FromQuery] PageQuery page, [FromQuery(Name = "status")] string? statusFilter = null)
    {
        return await orders.ListAsync(page);
    }

    [HttpDelete("{id:int}")]
    [Authorize(Roles = "Admin,Support")]
    public async Task<IActionResult> Cancel(int id, CancellationToken ct)
    {
        await orders.CancelAsync(id);
        return NoContent();
    }

    [HttpPost("{id:int}/pay")]
    public async Task<ActionResult<bool>> Pay(int id)
    {
        var paid = await mediator.Send(new PayOrderCommand(id));
        return paid ? Ok(true) : Conflict();
    }

    [HttpPost("import")]
    public IActionResult Import(IFormFile file, [FromForm] string source) => Accepted();
}
