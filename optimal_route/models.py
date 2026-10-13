from django.db import models


class FuelStation(models.Model):
    opis_id = models.IntegerField()
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=128)
    state = models.CharField(max_length=2, db_index=True)
    price = models.DecimalField(max_digits=6, decimal_places=3)
    latitude = models.FloatField(db_index=True)
    longitude = models.FloatField(db_index=True)

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
