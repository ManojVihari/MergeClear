using System.ComponentModel.DataAnnotations;
using Dapper;
using Microsoft.AspNetCore.Http.HttpResults;
using Microsoft.AspNetCore.Mvc;
using Microsoft.EntityFrameworkCore;
using StackExchange.Redis;
using System.Data;

namespace Minimal.Features;

public class Todo
{
    public int Id { get; set; }
    [Required, StringLength(100)]
    public string Title { get; set; } = "";
    public bool Done { get; set; }
}

public class TodoDb : DbContext
{
    public TodoDb(DbContextOptions<TodoDb> options) : base(options) { }
    public DbSet<Todo> Todos => Set<Todo>();
}

public record TodoQuery([FromQuery] bool? Done, [FromQuery(Name = "q")] string? Search, [FromHeader(Name = "X-Tenant")] string? Tenant);
public record CreateTodo([property: Required, MaxLength(100)] string Title);

public static class TodoEndpoints
{
    public static void Map(RouteGroupBuilder group)
    {
        group.MapGet("/", async ([AsParameters] TodoQuery query, TodoDb db, IDatabase redis) =>
        {
            var cached = await redis.StringGetAsync("todos:all");
            if (cached.HasValue) return Results.Ok(cached.ToString());
            var todos = await db.Todos.Where(t => t.Done == query.Done).ToListAsync();
            await redis.StringSetAsync("todos:all", "x");
            return Results.Ok(todos);
        });

        group.MapGet("/{id:int}", async Task<Results<Ok<Todo>, NotFound>> (int id, TodoDb db) =>
            await db.Todos.FindAsync(id) is Todo todo ? TypedResults.Ok(todo) : TypedResults.NotFound());

        group.MapPost("/", async (CreateTodo input, TodoDb db) =>
        {
            var todo = new Todo { Title = input.Title };
            db.Todos.Add(todo);
            await db.SaveChangesAsync();
            return TypedResults.Created($"/todos/{todo.Id}", todo);
        });

        group.MapGet("/report", async (IDbConnection connection, int year) =>
        {
            var rows = await connection.QueryAsync<Todo>("SELECT * FROM todos t JOIN reports r ON r.todo_id = t.id WHERE r.year = @year", new { year });
            return rows;
        });

        group.MapDelete("/{id:int}", (int id, TodoDb db) =>
        {
            db.Database.ExecuteSqlRaw("DELETE FROM todos WHERE id = {0}", id);
            return Results.NoContent();
        }).RequireAuthorization();
    }
}
