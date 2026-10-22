using System.ComponentModel.DataAnnotations;
using System.Text.Json.Serialization;
using Shop.Data.Entities;

namespace Shop.Api.Models;

public record LineItemDto([Required, StringLength(40, MinimumLength = 2)] string Sku, [Range(1, 100)] int Quantity);

public record CreateOrderRequest(
    [Required, EmailAddress] string CustomerEmail,
    [Required, MinLength(1), MaxLength(20)] List<LineItemDto> Lines,
    PaymentMethod PaymentMethod = PaymentMethod.Card,
    string? Note = null);

public enum PaymentMethod { Card, Paypal }

public class OrderDto
{
    public int Id { get; set; }
    public OrderStatus Status { get; set; }
    public string CustomerEmail { get; set; } = string.Empty;
    [JsonPropertyName("lines")]
    public List<LineItemDto> Lines { get; set; } = new();
    [JsonIgnore]
    public string InternalNotes { get; set; } = string.Empty;
}

public class PagedResult<T>
{
    public List<T> Items { get; set; } = new();
    public int Total { get; set; }
    public int Page { get; set; }
}

public class PageQuery
{
    [Range(1, 1000)]
    public int Page { get; set; } = 1;
    [Range(1, 100)]
    public int PageSize { get; set; } = 20;
}
