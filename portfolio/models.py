from django.db import models
from decimal import Decimal

class Portfolio(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    initial_balance = models.DecimalField(max_digits=15, decimal_places=2)
    current_balance = models.DecimalField(max_digits=15, decimal_places=2)
    total_value = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    snaptrade_user_id = models.CharField(max_length=100, blank=True)
    snaptrade_account_id = models.CharField(max_length=100, blank=True)
    snaptrade_user_key = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "portfolios"
        ordering = ["name"]

    def calculate_total_value(self):
        positions_value = sum(
            position.current_value for position in self.positions.filter(quantity__gt=0)
        )
        self.total_value = self.current_balance + positions_value
        return self.total_value

    def get_current_positions(self):
        return self.positions.filter(quantity__gt=0).select_related("stock")

class Position(models.Model):
    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="positions"
    )
    stock = models.ForeignKey("trading.Stock", on_delete=models.CASCADE)
    quantity = models.IntegerField(default=0)
    average_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    current_price = models.DecimalField(

        max_digits=12, decimal_places=4, null=True, blank=True
    )
    current_value = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    unrealized_pnl = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    unrealized_pnl_percent = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    last_updated = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "positions"
        unique_together = ("portfolio", "stock")
        ordering = ["-current_value"]

    def update_current_value(self, current_price=None):
        if current_price:
            self.current_price = current_price

        if self.current_price and self.quantity > 0:
            self.current_value = Decimal(str(self.quantity)) * self.current_price
            cost_basis = Decimal(str(self.quantity)) * self.average_cost
            self.unrealized_pnl = self.current_value - cost_basis

            if cost_basis > 0:
                self.unrealized_pnl_percent = (self.unrealized_pnl / cost_basis) * 100

        else:
            self.current_value = 0
            self.unrealized_pnl = 0
            self.unrealized_pnl_percent = 0

    def add_shares(self, quantity, price):
        if self.quantity > 0:
            total_cost = (self.quantity * self.average_cost) + (quantity*price)
            total_shares = self.quantity + quantity
            self.average_cost = total_cost / total_shares

        else:
            self.average_cost = price

        self.quantity += quantity
        self.update_current_value(price)

    def remove_shares(self, quantity, price):
        if quantity >= self.quantity:
            self.quantity = 0
            self.average_cost = 0
            self.current_value = 0
            self.unrealized_pnl = 0
            self.unrealized_pnl_percent = 0
        else:
            self.quantity -= quantity
            self.update_current_value(price)

class Trade(models.Model):
    TRADE_TYPES = [
        ("BUY", "Buy"),
        ("SELL", "Sell"),
    ]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SUBMITTED", "Submitted"),
        ("FILLED", "Filled"),
        ("PARTIALLY_FILLED", "Partially Filled"),
        ("CANCELLED", "Cancelled"),
        ("REJECTED", "Rejected"),
    ]

    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="trades"
    )
    stock = models.ForeignKey(
        "trading.Stock", on_delete=models.CASCADE
    )
    trade_type = models.CharField(max_length=4, choices=TRADE_TYPES)
    quantity = models.IntegerField()
    price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
        )
    filled_quantity = models.IntegerField(default=0)
    filled_price = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
        )
    order_value = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    external_order_id = models.CharField(max_length=100, blank=True)
    snaptrade_order_id = models.CharField(max_length=100, blank=True)
    commision = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    error_message = models.TextField(blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    filled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "trades"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["portfolio", "status"]),
            models.Index(fields=["status", "created_at"])
        ]

class PerformanceMetric(models.Model):
    portfolio = models.ForeignKey(
        Portfolio, on_delete=models.CASCADE, related_name="performance_metrics"
    )
    date = models.DateField()
    total_value = models.DecimalField(max_digits=15, decimal_places=2)
    cash_value = models.DecimalField(max_digits=15, decimal_places=2)
    positions_value = models.DecimalField(max_digits=15, decimal_places=2)
    daily_return = models.DecimalField(
        max_digits=8, decimal_places=6, null=True, blank=True
    )
    cumulative_return = models.DecimalField(
        max_digits=8, decimal_places=6, null=True, blank=True
    )
    total_trades = models.IntegerField(default=0)
    winning_trades = models.IntegerField(default=0)
    losing_trades = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "performance_metrics"
        unique_together = ("portfolio", "date")
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["portfolio", "date"])
        ]