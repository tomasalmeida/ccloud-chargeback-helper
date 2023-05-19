import datetime
import decimal
from dataclasses import dataclass, field
from typing import Dict, List
import pandas as pd
from time import sleep, time
from data_processing.data_handlers.billing_api_handler import BILLING_API_COLUMNS, CCloudBillingHandler
from data_processing.data_handlers.ccloud_api_handler import CCloudObjectsHandler
from data_processing.data_handlers.prom_metrics_handler import (
    METRICS_API_COLUMNS,
    METRICS_API_PROMETHEUS_QUERIES,
    PrometheusMetricsDataHandler,
)
from data_processing.data_handlers.types import AbstractDataHandler
from prometheus_processing.custom_collector import TimestampedCollector
from prometheus_processing.notifier import NotifierAbstract, Observer


class ChargebackColumnNames:
    TS = "Timestamp"
    PRINCIPAL = "Principal"
    KAFKA_CLUSTER = "KafkaID"
    USAGE_COST = "UsageCost"
    SHARED_COST = "SharedCost"

    def override_column_names(self, key, value):
        object.__setattr__(self, key, value)

    def all_column_values(self) -> List:
        return [y for x, y in vars(self).items()]


CHARGEBACK_COLUMNS = ChargebackColumnNames()

chargeback_prom_metrics = TimestampedCollector(
    "confluent_cloud_chargeback_details",
    "Approximate Chargeback Distribution details for costs w.r.t contextual access within CCloud",
    ["principal", "cost_type"],
    in_begin_timestamp=datetime.datetime.now(),
)


@dataclass(kw_only=True)
class CCloudChargebackHandler(AbstractDataHandler, Observer):
    billing_dataset: CCloudBillingHandler = field(init=True)
    objects_dataset: CCloudObjectsHandler = field(init=True)
    metrics_dataset: PrometheusMetricsDataHandler = field(init=True)
    start_date: datetime.datetime = field(init=True)
    days_per_query: int = field(default=7)

    chargeback_dataset: Dict = field(init=False, repr=False, default_factory=dict)
    curr_export_datetime: datetime.datetime = field(init=False)

    def __post_init__(self) -> None:
        """Initialize the Chargeback handler:
        * Set the Start Date as UTC zero and time.min to convert the date to midnight of UTC
        * Calculate the data and save it in memory as a dict
        * attach this class to the Prom scraper which is also a notifier
        * set the exported datetime in memory for stepping through the data every scrape
        """
        AbstractDataHandler.__init__(self)
        Observer.__init__(self)
        self.start_date = self.start_date.replace(tzinfo=datetime.timezone.utc).combine(time=datetime.time.min)
        # Calculate the end_date from start_date plus number of days per query
        end_date = self.start_date + datetime.timedelta(days=self.days_per_query)
        self.read_all(start_date=self.start_date, end_date=end_date)
        self.attach(chargeback_prom_metrics)
        self.curr_export_datetime = self.start_date - datetime.timedelta(hours=1)
        self.update(notifier=chargeback_prom_metrics)

    def expose_prometheus_metrics(self, ts_filter: pd.Timestamp):
        """Set and expose the metrics to the prom collector as a Gauge.

        Args:
            ts_filter (pd.Timestamp): This Timestamp allows us to filter the data from the entire data set
            to a specific timestamp and expose it to the prometheus collector
        """
        out = self.__get_dataset_for_exact_timestamp(
            dataset=self.chargeback_dataset, ts_column_name=CHARGEBACK_COLUMNS.TS, time_slice=ts_filter
        )
        for df_row in out.itertuples(name="ChargeBackData"):
            principal_id = df_row[CHARGEBACK_COLUMNS.PRINCIPAL]
            for k, v in df_row._asdict().items():
                if k not in [CHARGEBACK_COLUMNS.TS, CHARGEBACK_COLUMNS.PRINCIPAL]:
                    chargeback_prom_metrics.labels(principal_id, k).set(v)

    def update(self, notifier: NotifierAbstract) -> None:
        """This is the Observer class method implementation that helps us step through the next timestamp in sequence.
        The Data for next timestamp is also populated in the Gauge implementation using this method.
        It also tracks the currently exported timestamp in Observer as well as update it to the Notifier.

        Args:
            notifier (NotifierAbstract): This objects is used to get updates from the notifier that the collection for on timestamp is complete and the dataset should be refreshed for the next timestamp.
        """
        next_ts = self.__generate_next_timestamp(curr_date=self.curr_export_datetime)
        next_ts_in_dt = next_ts.to_pydatetime(warn=False)
        notifier.set_timestamp(curr_timestamp=next_ts_in_dt)
        self.expose_prometheus_metrics(ts_filter=next_ts)
        self.curr_export_datetime = next_ts_in_dt

    def read_all(self, start_date: datetime.datetime, end_date: datetime.datetime, **kwargs):
        """Iterate through all the timestamps in the datetime range and calculate the chargeback for that timestamp

        Args:
            start_date (datetime.datetime): Inclusive datetime for the period beginning 
            end_date (datetime.datetime): Exclusive datetime for the period ending 
        """
        for time_slice_item in self._generate_date_range_per_row(start_date=start_date, end_date=end_date):
            self.compute_output(time_slice=time_slice_item)

    def cleanup_old_data(self):
        """Cleanup the older dataset from the chargeback object and prevent it from using too much memory
        """
        for (k1, k2), (_, _, _) in self.chargeback_dataset.copy().items():
            if k2 < self.start_date:
                del self.chargeback_dataset[(k1, k2)]

    def read_next_dataset(self):
        """Calculate chargeback data fom the next timeslot. This should be used when the current_export_datetime is running very close to the days_per_query end_date.
        """
        self.start_date = self.start_date + datetime.timedelta(days=self.days_per_query)
        end_date = self.start_date + datetime.timedelta(days=self.days_per_query)
        self.cleanup_old_data()
        self.read_all(start_date=self.start_date, end_date=end_date)

    def __add_cost_to_chargeback_dataset(
        self,
        principal: str,
        time_slice: datetime.datetime,
        product_type_name: str,
        additional_usage_cost: decimal.Decimal = decimal.Decimal(0),
        additional_shared_cost: decimal.Decimal = decimal.Decimal(0),
    ):
        """Internal chargeback Data structure to hold all the calculated chargeback data in memory. 
        As the column names & values were needed to be dynamic, we did not use a dataframe here for ease of use. 

        Args:
            principal (str): The Principal used for Chargeback Aggregation -- Primary Complex key
            time_slice (datetime.datetime): datetime of the Hour used for chargeback aggregation -- Primary complex key
            product_type_name (str): The different product names available in CCloud for aggregation
            additional_usage_cost (decimal.Decimal, optional): Is the cost Usage cost for that product type and what is the total usage cost for that duration? Defaults to decimal.Decimal(0).
            additional_shared_cost (decimal.Decimal, optional): Is the cost Shared cost for that product type and what is the total shared cost for that duration. Defaults to decimal.Decimal(0).
        """
        if (principal, time_slice) in self.chargeback_dataset:
            u, s, detailed_split = self.chargeback_dataset[(principal, time_slice)]
            detailed_split[product_type_name] = (
                detailed_split.get(product_type_name, decimal.Decimal(0))
                + additional_shared_cost
                + additional_usage_cost
            )
            self.chargeback_dataset[(principal, time_slice)] = (
                u + additional_usage_cost,
                s + additional_shared_cost,
                detailed_split,
            )
        else:
            detailed_split = dict()
            detailed_split[product_type_name] = additional_shared_cost + additional_usage_cost
            self.chargeback_dataset[(principal, time_slice)] = (
                additional_usage_cost,
                additional_shared_cost,
                detailed_split,
            )

    def get_chargeback_dataset(self):
        for (k1, k2), (usage, shared, extended) in self.chargeback_dataset.items():
            temp_dict = {
                CHARGEBACK_COLUMNS.PRINCIPAL: k1,
                CHARGEBACK_COLUMNS.TS: k2,
                CHARGEBACK_COLUMNS.USAGE_COST: usage,
                CHARGEBACK_COLUMNS.SHARED_COST: shared,
            }
            temp_dict.update(extended)
            yield temp_dict

    def get_chargeback_dataframe(self) -> pd.DataFrame:
        """Generate pandas Dataframe for the Chargeback data available in memory within attribute chargeback_dataset

        Returns:
            pd.DataFrame: _description_
        """
        # TODO: Getting this dataframe is amazingly under optimized albeit uses yield.
        # Uses an intermittent list of dict conversion and then another step to convert to dataframe
        # No clue at the moment on how to improve this.
        return pd.DataFrame.from_records(
            self.get_chargeback_dataset(), index=[CHARGEBACK_COLUMNS.PRINCIPAL, CHARGEBACK_COLUMNS.TS]
        )

    def compute_output(
        self, time_slice: datetime.datetime,
    ):
        """The core calculation method. This method aggregates all the costs on a per product type basis for every principal per hour and appends that calculated dataset in chargeback_dataset object attribute

        Args:
            time_slice (datetime.datetime): The exact timestamp for which the compute will happen
        """
        billing_data = self.billing_dataset.get_dataset_for_time_slice(time_slice=time_slice)
        metrics_data = self.metrics_dataset.get_dataset_for_time_slice(time_slice=time_slice)
        for bill_row in billing_data.itertuples(index=True, name="BillingRow"):
            row_ts, row_env, row_cid, row_pname, row_ptype = (
                bill_row.Index[0].to_pydatetime(),
                bill_row.Index[1],
                bill_row.Index[2],
                bill_row.Index[3],
                bill_row.Index[4],
            )

            df_time_slice = pd.Timestamp(time_slice, tz="UTC")

            row_cname = getattr(bill_row, BILLING_API_COLUMNS.cluster_name)
            row_cost = getattr(bill_row, BILLING_API_COLUMNS.calc_split_total)
            if row_ptype == "KafkaBase":
                # GOAL: Split Cost equally across all the SA/Users that have API Keys for that Kafka Cluster
                # Find all active Service Accounts/Users For kafka Cluster using the API Keys in the system.
                sa_count = self.objects_dataset.cc_api_keys.find_sa_count_for_clusters(cluster_id=row_cid)
                if len(sa_count) > 0:
                    splitter = len(sa_count)
                    # Add Shared Cost for all active SA/Users in the cluster and split it equally
                    for sa_name, sa_api_key_count in sa_count.items():
                        self.chargeback_dataset.__add_cost_to_chargeback_dataset(
                            principal=sa_name,
                            time_slice=row_ts,
                            product_type_name=row_ptype,
                            additional_shared_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No API Keys available for cluster {row_cid}. Attributing {row_ptype} for {row_cid} as Cluster Shared Cost"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            elif row_ptype == "KafkaNetworkRead":
                # GOAL: Split cost across all the consumers to that cluster as a ratio of consumption performed.
                # Read Depends in the Response_Bytes Metric Only
                col_name = METRICS_API_PROMETHEUS_QUERIES.response_bytes_name
                # filter metrics for data that has some consumption > 0 , and then find all rows with index
                # with that timestamp and that specific kafka cluster.
                try:
                    subset = metrics_data[metrics_data[col_name] > 0][
                        [
                            METRICS_API_COLUMNS.timestamp,
                            METRICS_API_COLUMNS.cluster_id,
                            METRICS_API_COLUMNS.principal_id,
                            col_name,
                        ]
                    ]
                    metric_rows = subset[
                        (subset[METRICS_API_COLUMNS.timestamp] == df_time_slice)
                        & (subset[METRICS_API_COLUMNS.cluster_id] == row_cid)
                    ]
                except KeyError:
                    metric_rows = pd.DataFrame()
                if not metric_rows.empty:
                    # Find the total consumption during that time slice
                    agg_data = metric_rows[[col_name]].agg(["sum"])
                    # add the Ratio consumption column by dividing every row by total consumption.
                    metric_rows[f"{col_name}_ratio"] = metric_rows[col_name].transform(
                        lambda x: decimal.Decimal(x) / decimal.Decimal(agg_data.loc[["sum"]][col_name])
                    )
                    # for every filtered Row , add consumption
                    for metric_row in metric_rows.itertuples(index=True, name="MetricsRow"):
                        self.chargeback_dataset.__add_cost_to_chargeback_dataset(
                            getattr(metric_row, METRICS_API_COLUMNS.principal_id),
                            row_ts,
                            row_ptype,
                            additional_usage_cost=decimal.Decimal(row_cost)
                            * decimal.Decimal(getattr(metric_row, f"{col_name}_ratio")),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- Could not map {row_ptype} for {row_cid}. Attributing as Cluster Shared Cost for cluster {row_cid}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost),
                    )
            elif row_ptype == "KafkaNetworkWrite":
                # GOAL: Split cost across all the producers to that cluster as a ratio of production performed.
                # Read Depends in the Response_Bytes Metric Only
                col_name = METRICS_API_PROMETHEUS_QUERIES.request_bytes_name
                # filter metrics for data that has some consumption > 0 , and then find all rows with index
                # with that timestamp and that specific kafka cluster.
                try:
                    subset = metrics_data[metrics_data[col_name] > 0][
                        [
                            METRICS_API_COLUMNS.timestamp,
                            METRICS_API_COLUMNS.cluster_id,
                            METRICS_API_COLUMNS.principal_id,
                            col_name,
                        ]
                    ]
                    metric_rows = subset[
                        (subset[METRICS_API_COLUMNS.timestamp] == df_time_slice)
                        & (subset[METRICS_API_COLUMNS.cluster_id] == row_cid)
                    ]
                except KeyError:
                    metric_rows = pd.DataFrame()
                if not metric_rows.empty:
                    # print(metric_rows.info())
                    # Find the total consumption during that time slice
                    agg_value = metric_rows[[col_name]].agg(["sum"]).loc["sum", col_name]
                    # add the Ratio consumption column by dividing every row by total consumption.
                    metric_rows[f"{col_name}_ratio"] = (
                        metric_rows[col_name]
                        .transform(lambda x: decimal.Decimal(x) / decimal.Decimal(agg_value))
                        .to_list()
                    )
                    # for every filtered Row , add consumption
                    for metric_row in metric_rows.itertuples(index=True, name="MetricsRow"):
                        self.__add_cost_to_chargeback_dataset(
                            getattr(metric_row, METRICS_API_COLUMNS.principal_id),
                            row_ts,
                            row_ptype,
                            additional_usage_cost=decimal.Decimal(row_cost)
                            * decimal.Decimal(getattr(metric_row, f"{col_name}_ratio")),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- Could not map {row_ptype} for {row_cid}. Attributing as Cluster Shared Cost for cluster {row_cid}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost),
                    )
            elif row_ptype == "KafkaNumCKUs":
                # GOAL: Split into 2 Categories --
                #       Common Charge -- Flat 50% of the cost Divided across all clients active in that duration.
                #       Usage Charge  -- 50% of the cost split variably by the amount of data produced + consumed by the SA/User
                common_charge_ratio = 0.30
                usage_charge_ratio = 0.70
                # Common Charge will be added as a ratio of the count of API Keys created for each service account.
                sa_count = self.objects_dataset.cc_api_keys.find_sa_count_for_clusters(cluster_id=row_cid)
                if len(sa_count) > 0:
                    splitter = len(sa_count)
                    # total_api_key_count = len(
                    #     [x for x in self.cc_objects.cc_api_keys.api_keys.values() if x.cluster_id != "cloud"]
                    # )
                    for sa_name, sa_api_key_count in sa_count.items():
                        self.__add_cost_to_chargeback_dataset(
                            sa_name,
                            row_ts,
                            row_ptype,
                            additional_shared_cost=(decimal.Decimal(row_cost) * decimal.Decimal(common_charge_ratio))
                            / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No API Keys were found for cluster {row_cid}. Attributing Common Cost component for {row_ptype} as Cluster Shared Cost for cluster {row_cid}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid,
                        row_ts,
                        row_ptype,
                        additional_shared_cost=decimal.Decimal(row_cost) * decimal.Decimal(common_charge_ratio),
                    )
                # filter metrics for data that has some consumption > 0 , and then find all rows with index
                # with that timestamp and that specific kafka cluster.
                try:
                    metric_rows = metrics_data[
                        (metrics_data[METRICS_API_COLUMNS.timestamp] == df_time_slice)
                        & (metrics_data[METRICS_API_COLUMNS.cluster_id] == row_cid)
                    ]
                except KeyError:
                    metric_rows = pd.DataFrame()
                # Usage Charge
                if not metric_rows.empty:
                    # Find the total consumption during that time slice
                    query_dataset = [
                        METRICS_API_PROMETHEUS_QUERIES.request_bytes_name,
                        METRICS_API_PROMETHEUS_QUERIES.response_bytes_name,
                    ]

                    agg_data = metric_rows[query_dataset].agg(["sum"])
                    # add the Ratio consumption column by dividing every row by total consumption.
                    for metric_item in query_dataset.values():
                        metric_rows[f"{metric_item}_ratio"] = metric_rows[metric_item].transform(
                            lambda x: decimal.Decimal(x) / decimal.Decimal(agg_data.loc[["sum"]][metric_item])
                        )
                    # for every filtered Row , add consumption
                    for metric_row in metric_rows.itertuples(index=True, name="MetricsRow"):
                        req_cost = (
                            row_cost
                            / len(query_dataset)
                            * getattr(metric_row, f"{METRICS_API_PROMETHEUS_QUERIES.request_bytes_name}_ratio")
                        )
                        res_cost = (
                            row_cost
                            / len(query_dataset)
                            * getattr(metric_row, f"{METRICS_API_PROMETHEUS_QUERIES.response_bytes_name}_ratio")
                        )
                        self.__add_cost_to_chargeback_dataset(
                            getattr(metric_row, METRICS_API_COLUMNS.principal_id),
                            row_ts,
                            row_ptype,
                            # additional_shared_cost=(common_charge_ratio * row_cost) / metric_rows.size,
                            additional_usage_cost=decimal.Decimal(usage_charge_ratio)
                            * (decimal.Decimal(req_cost) + decimal.Decimal(res_cost)),
                        )
                else:
                    if len(sa_count) > 0:
                        print(
                            f"Row TS: {str(row_ts)} -- No Production/Consumption activity for cluster {row_cid}. Splitting Usage Ratio for {row_ptype} across all Service Accounts as Shared Cost"
                        )
                        splitter = len(sa_count)
                        for sa_name, sa_api_key_count in sa_count.items():
                            self.__add_cost_to_chargeback_dataset(
                                sa_name,
                                row_ts,
                                row_ptype,
                                additional_shared_cost=(
                                    decimal.Decimal(row_cost) * decimal.Decimal(usage_charge_ratio)
                                )
                                / decimal.Decimal(splitter),
                            )
                    else:
                        print(
                            f"Row TS: {str(row_ts)} -- No Production/Consumption activity for cluster {row_cid} and no API Keys found for the cluster {row_cid}. Attributing Common Cost component for {row_ptype} as Cluster Shared Cost for cluster {row_cid}"
                        )
                        self.__add_cost_to_chargeback_dataset(
                            row_cid,
                            row_ts,
                            row_ptype,
                            additional_shared_cost=decimal.Decimal(row_cost) * decimal.Decimal(usage_charge_ratio),
                        )
            elif row_ptype in ["KafkaPartition", "KafkaStorage"]:
                # GOAL: Split cost across all the API Key holders for the specific Cluster
                # Find all active Service Accounts/Users For kafka Cluster using the API Keys in the system.
                sa_count = self.objects_dataset.cc_api_keys.find_sa_count_for_clusters(cluster_id=row_cid)
                if len(sa_count) > 0:
                    splitter = len(sa_count)
                    # Add Shared Cost for all active SA/Users in the cluster and split it equally
                    for sa_name, sa_api_key_count in sa_count.items():
                        self.__add_cost_to_chargeback_dataset(
                            sa_name,
                            row_ts,
                            row_ptype,
                            additional_shared_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No API Keys available for cluster {row_cid}. Attributing {row_ptype}  for {row_cid} as Cluster Shared Cost"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            elif row_ptype == "EventLogRead":
                # GOAL: Split Audit Log read cost across all the Service Accounts + Users that are created in the Org
                # Find all active Service Accounts/Users in the system.
                active_identities = list(self.objects_dataset.cc_sa.sa.keys()) + list(
                    objects_dataset.cc_users.users.keys()
                )
                splitter = len(active_identities)
                # Add Shared Cost for all active SA/Users in the cluster and split it equally
                for identity_item in active_identities:
                    self.__add_cost_to_chargeback_dataset(
                        identity_item,
                        row_ts,
                        row_ptype,
                        additional_shared_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                    )
            elif row_ptype == "ConnectCapacity":
                # GOAL: Split the Connect Cost across all the connect Service Accounts active in the cluster
                active_identities = set(
                    [
                        y.owner_id
                        for x, y in self.cc_objects.cc_connectors.connectors.items()
                        if y.cluster_id == row_cid
                    ]
                )
                if len(active_identities) > 0:
                    splitter = len(active_identities)
                    for identity_item in active_identities:
                        self.chargeback_dataset.__add_cost_to_chargeback_dataset(
                            identity_item,
                            row_ts,
                            row_ptype,
                            additional_shared_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No Connector Details were found. Attributing as Shared Cost for Kafka Cluster {row_cid}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            elif row_ptype in ["ConnectNumTasks", "ConnectThroughput"]:
                # GOAL: Cost will be assumed by the owner of the connector
                # There will be only one active Identity but we will still loop on the identity for consistency
                # The conditions are checking for the specific connector in an environment and trying to find its owner.
                active_identities = set(
                    [
                        y.owner_id
                        for x, y in self.objects_dataset.cc_connectors.connectors.items()
                        if y.env_id == row_env and y.connector_name == row_cname
                    ]
                )
                if len(active_identities) > 0:
                    splitter = len(active_identities)
                    for identity_item in active_identities:
                        self.__add_cost_to_chargeback_dataset(
                            identity_item,
                            row_ts,
                            row_ptype,
                            additional_usage_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No Connector Details were found. Using the Connector {row_cid} and adding cost as Shared Cost"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            elif row_ptype == "ClusterLinkingPerLink":
                # GOAL: Cost will be assumed by the Logical Cluster ID listed in the Billing API
                self.__add_cost_to_chargeback_dataset(
                    row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                )
            elif row_ptype == "ClusterLinkingRead":
                # GOAL: Cost will be assumed by the Logical Cluster ID listed in the Billing API
                self.__add_cost_to_chargeback_dataset(
                    row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                )
            elif row_ptype == "ClusterLinkingWrite":
                # GOAL: Cost will be assumed by the Logical Cluster ID listed in the Billing API
                self.__add_cost_to_chargeback_dataset(
                    row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                )
            elif row_ptype in ["GovernanceBase", "SchemaRegistry"]:
                # GOAL: Cost will be equally spread across all the Kafka Clusters existing in this CCloud Environment
                active_identities = set(
                    [y.cluster_id for x, y in self.objects_dataset.cc_clusters.cluster.items() if y.env_id == row_env]
                )
                if len(active_identities) > 0:
                    splitter = len(active_identities)
                    for identity_item in active_identities:
                        self.chargeback_dataset.__add_cost_to_chargeback_dataset(
                            identity_item,
                            row_ts,
                            row_ptype,
                            additional_usage_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No Kafka Clusters present within the environment. Attributing as Shared Cost to {row_env}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_env, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            elif row_ptype == "KSQLNumCSUs":
                # GOAL: Cost will be assumed by the ksql Service Account/User being used by the ksqldb cluster
                # There will be only one active Identity but we will still loop on the identity for consistency
                # The conditions are checking for the specific ksqldb cluster in an environment and trying to find its owner.
                active_identities = set(
                    [
                        y.owner_id
                        for x, y in self.objects_dataset.cc_ksqldb_clusters.ksqldb_clusters.items()
                        if y.cluster_id == row_cid
                    ]
                )
                if len(active_identities) > 0:
                    splitter = len(active_identities)
                    for identity_item in active_identities:
                        self.__add_cost_to_chargeback_dataset(
                            identity_item,
                            row_ts,
                            row_ptype,
                            additional_usage_cost=decimal.Decimal(row_cost) / decimal.Decimal(splitter),
                        )
                else:
                    print(
                        f"Row TS: {str(row_ts)} -- No KSQL Cluster Details were found. Attributing as Shared Cost for ksqlDB cluster ID {row_cid}"
                    )
                    self.__add_cost_to_chargeback_dataset(
                        row_cid, row_ts, row_ptype, additional_shared_cost=decimal.Decimal(row_cost)
                    )
            else:
                print("=" * 80)
                print(
                    f"Row TS: {str(row_ts)} -- No Chargeback calculation available for {row_ptype}. Please request for it to be added."
                )
                print("=" * 80)

