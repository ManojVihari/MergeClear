using Microsoft.EntityFrameworkCore;
using Shop.Data.Entities;

namespace Shop.Data;

public class ShopDbContext : DbContext
{
    public ShopDbContext(DbContextOptions<ShopDbContext> options) : base(options) { }

    public DbSet<Order> Orders => Set<Order>();
    public DbSet<OrderLine> OrderLines { get; set; } = null!;
    public DbSet<AuditLog> AuditLogs { get; set; } = null!;

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<Order>().ToTable("orders");
        modelBuilder.Entity<Order>().Property(o => o.Note).HasMaxLength(500);
        modelBuilder.Entity<AuditLog>(e =>
        {
            e.ToTable("audit_log");
        });
    }
}
