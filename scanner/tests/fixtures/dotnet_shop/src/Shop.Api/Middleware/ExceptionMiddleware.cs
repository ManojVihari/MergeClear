using Shop.Api.Services;

namespace Shop.Api.Middleware;

public class ExceptionMiddleware
{
    private readonly RequestDelegate _next;
    public ExceptionMiddleware(RequestDelegate next) { _next = next; }

    public async Task InvokeAsync(HttpContext context)
    {
        try
        {
            await _next(context);
        }
        catch (NotFoundException)
        {
            context.Response.StatusCode = StatusCodes.Status404NotFound;
        }
        catch (PaymentDeclinedException)
        {
            context.Response.StatusCode = 402;
        }
        catch (InvalidOperationException)
        {
            context.Response.StatusCode = StatusCodes.Status409Conflict;
        }
    }
}
