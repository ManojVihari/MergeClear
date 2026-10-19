namespace Shop.Api.Clients;

public interface IPaymentClient
{
    Task<bool> ChargeAsync(int orderId, decimal amount);
}

public class PaymentClient : IPaymentClient
{
    private readonly HttpClient _http;

    public PaymentClient(HttpClient http) => _http = http;

    public async Task<bool> ChargeAsync(int orderId, decimal amount)
    {
        var response = await _http.PostAsJsonAsync($"/v1/charges/{orderId}", new { amount });
        return response.IsSuccessStatusCode;
    }
}
