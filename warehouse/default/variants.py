from warehouse.default.core import Warehouse


__all__ = ["WarehouseSmall", "WarehouseMedium", "WarehouseLarge"]


class WarehouseSmall(Warehouse):
    def __init__(self) -> None:
        self.num_agents: int = 2
        self.num_requests: int = 2
        self.area_dimension: float = 8.0

        super(WarehouseSmall, self).__init__(
            num_agents=self.num_agents,
            num_requests=self.num_requests,
            area_dimension=self.area_dimension,
            agent_init_positions=[[2.0, 2.0], [6.0, 8.0]],
            pickup_racks_arrangement=[4.0],
            episode_duration_s=200,
            pickup_wait_duration_s=40,
        )


class WarehouseMedium(Warehouse):
    def __init__(self) -> None:
        self.num_agents: int = 4
        self.num_requests: int = 4
        self.area_dimension: float = 12.0

        super(WarehouseMedium, self).__init__(
            num_agents=self.num_agents,
            num_requests=self.num_requests,
            area_dimension=self.area_dimension,
            agent_init_positions=[[2.0, 2.0], [2.0, 10.0], [10.0, 2.0], [10.0, 10.0]],
            pickup_racks_arrangement=[4.0, 8.0],
            episode_duration_s=200,
            pickup_wait_duration_s=40,
        )


class WarehouseLarge(Warehouse):
    def __init__(self) -> None:
        self.num_agents: int = 16
        self.num_requests: int = 16
        self.area_dimension: float = 20.0

        super(WarehouseLarge, self).__init__(
            num_agents=self.num_agents,
            num_requests=self.num_requests,
            area_dimension=self.area_dimension,
            agent_init_positions=[
                [2.0, 2.0],
                [2.0, 6.0],
                [2.0, 10.0],
                [2.0, 14.0],
                [2.0, 18.0],
                [18.0, 2.0],
                [18.0, 6.0],
                [18.0, 10.0],
                [18.0, 14.0],
                [18.0, 18.0],
                [6.0, 2.0],
                [10.0, 2.0],
                [14.0, 2.0],
                [6.0, 18.0],
                [10.0, 18.0],
                [14.0, 18.0],
            ],
            pickup_racks_arrangement=[4.0, 8.0, 12.0, 16.0],
            episode_duration_s=200,
            pickup_wait_duration_s=40,
        )