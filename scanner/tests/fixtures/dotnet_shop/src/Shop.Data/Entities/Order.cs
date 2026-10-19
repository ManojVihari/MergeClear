using System.ComponentModel.DataAnnotations;
using System.ComponentModel.DataAnnotations.Schema;

namespace Shop.Data.Entities;

public enum OrderStatus { New = 0, Paid = 1, Cancelled = 2 }

public class Order
{
    public int Id { get; set; }

    [Required, MaxLength(120)]
    public string CustomerEmail { get; set; } = string.Empty;

    public OrderStatus Status { get; set; }

    public string? Note { get; set; }

    public List<OrderLine> Lines { get; set; } = new();
}

[Table("order_lines")]
public class OrderLine
{
    [Key]
    public int LineId { get; set; }

    public int OrderId { get; set; }

    public string Sku { get; set; } = string.Empty;

    public int Quantity { get; set; }

    public Order? Order { get; set; }
}

public class AuditLog
{
    public int Id { get; set; }
    public int OrderId { get; set; }
    public string Action { get; set; } = string.Empty;
}
