using Microsoft.EntityFrameworkCore;
using Shop.Data.Entities;

namespace Shop.Data.Repositories;

public interface IRepository<T> where T : class
{
    Task<T?> GetByIdAsync(int id);
    Task<List<T>> ListAsync();
    Task AddAsync(T entity);
    Task RemoveAsync(T entity);
}

public class EfRepository<T> : IRepository<T> where T : class
{
    private readonly ShopDbContext _db;

    public EfRepository(ShopDbContext db) => _db = db;

    public async Task<T?> GetByIdAsync(int id) => await _db.Set<T>().FindAsync(id);

    public Task<List<T>> ListAsync() => _db.Set<T>().AsNoTracking().ToListAsync();

    public async Task AddAsync(T entity)
    {
        await _db.Set<T>().AddAsync(entity);
        await _db.SaveChangesAsync();
    }

    public async Task RemoveAsync(T entity)
    {
        _db.Set<T>().Remove(entity);
        await _db.SaveChangesAsync();
    }
}

public interface IOrderRepository
{
    Task<List<Order>> OpenOrdersAsync(string email);
}

public class OrderRepository : IOrderRepository
{
    private readonly ShopDbContext _context;

    public OrderRepository(ShopDbContext context) { _context = context; }

    public Task<List<Order>> OpenOrdersAsync(string email) =>
        _context.Orders.Include(o => o.Lines).Where(o => o.CustomerEmail == email && o.Status == OrderStatus.New).ToListAsync();
}
