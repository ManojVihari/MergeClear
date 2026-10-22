using FluentValidation;
using Shop.Api.Models;

namespace Shop.Api.Validators;

public class CreateOrderValidator : AbstractValidator<CreateOrderRequest>
{
    public CreateOrderValidator()
    {
        RuleFor(x => x.CustomerEmail).NotEmpty().MaximumLength(120);
        RuleFor(x => x.Note).MaximumLength(500);
    }
}
