"""Amazon Oracle Database Module."""

import importlib.util
import inspect
import logging
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar, Union

import boto3
import pandas as pd
import pyarrow as pa

from awswrangler import _data_types
from awswrangler import _databases as _db_utils
from awswrangler import exceptions
from awswrangler._config import apply_configs

__all__ = ["connect", "read_sql_query", "read_sql_table", "to_sql"]

_cx_Oracle_found = importlib.util.find_spec("cx_Oracle")
if _cx_Oracle_found:
    import cx_Oracle  # pylint: disable=import-error

_logger: logging.Logger = logging.getLogger(__name__)
FuncT = TypeVar("FuncT", bound=Callable[..., Any])


def _check_for_cx_Oracle(func: FuncT) -> FuncT:
    def inner(*args: Any, **kwargs: Any) -> Any:
        if not _cx_Oracle_found:
            raise ModuleNotFoundError(
                "You need to install cx_Oracle respectively the "
                "AWS Data Wrangler package with the `oracle` extra for using the oracle module"
            )
        return func(*args, **kwargs)

    inner.__doc__ = func.__doc__
    inner.__name__ = func.__name__
    inner.__setattr__("__signature__", inspect.signature(func))  # pylint: disable=no-member
    return inner  # type: ignore


def _validate_connection(con: "cx_Oracle.Connection") -> None:
    if not isinstance(con, cx_Oracle.Connection):
        raise exceptions.InvalidConnection(
            "Invalid 'conn' argument, please pass a "
            "cx_Oracle.Connection object. Use cx_Oracle.connect() to use "
            "credentials directly or wr.oracle.connect() to fetch it from the Glue Catalog."
        )


def _get_table_identifier(schema: Optional[str], table: str) -> str:
    schema_str = f'"{schema}".' if schema else ""
    table_identifier = f'{schema_str}"{table}"'
    return table_identifier


def _drop_table(cursor: "cx_Oracle.Cursor", schema: Optional[str], table: str) -> None:
    table_identifier = _get_table_identifier(schema, table)
    sql = f"""
BEGIN
   EXECUTE IMMEDIATE 'DROP TABLE {table_identifier}';
EXCEPTION
   WHEN OTHERS THEN
      IF SQLCODE != -942 THEN
         RAISE;
      END IF;
END;
"""
    _logger.debug("Drop table query:\n%s", sql)
    cursor.execute(sql)


def _does_table_exist(cursor: "cx_Oracle.Cursor", schema: Optional[str], table: str) -> bool:
    schema_str = f"TABLE_SCHEMA = '{schema}' AND" if schema else ""
    cursor.execute(f"SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE " f"{schema_str} TABLE_NAME = '{table}'")
    return len(cursor.fetchall()) > 0


def _create_table(
    df: pd.DataFrame,
    cursor: "cx_Oracle.Cursor",
    table: str,
    schema: str,
    mode: str,
    index: bool,
    dtype: Optional[Dict[str, str]],
    varchar_lengths: Optional[Dict[str, int]],
) -> None:
    if mode == "overwrite":
        _drop_table(cursor=cursor, schema=schema, table=table)
    elif _does_table_exist(cursor=cursor, schema=schema, table=table):
        return
    oracle_types: Dict[str, str] = _data_types.database_types_from_pandas(
        df=df,
        index=index,
        dtype=dtype,
        varchar_lengths_default="CLOB",
        varchar_lengths=varchar_lengths,
        converter_func=_data_types.pyarrow2oracle,
    )
    cols_str: str = "".join([f"{k} {v},\n" for k, v in oracle_types.items()])[:-2]
    table_identifier = _get_table_identifier(schema, table)
    sql = (
        f"CREATE TABLE {table_identifier} (\n{cols_str})"
    )
    _logger.debug("Create table query:\n%s", sql)
    cursor.execute(sql)


@_check_for_cx_Oracle
def connect(
    connection: Optional[str] = None,
    secret_id: Optional[str] = None,
    catalog_id: Optional[str] = None,
    dbname: Optional[str] = None,
    boto3_session: Optional[boto3.Session] = None,
    call_timeout: Optional[int] = 0,
) -> "cx_Oracle.Connection":
    """Return a cx_Oracle connection from a Glue Catalog Connection.

    https://github.com/oracle/python-cx_Oracle

    Note
    ----
    You MUST pass a `connection` OR `secret_id`.
    Here is an example of the secret structure in Secrets Manager:
    {
    "host":"oracle-instance-wrangler.dr8vkeyrb9m1.us-east-1.rds.amazonaws.com",
    "username":"test",
    "password":"test",
    "engine":"oracle",
    "port":"1433",
    "dbname": "mydb" # Optional
    }

    Parameters
    ----------
    connection : Optional[str]
        Glue Catalog Connection name.
    secret_id: Optional[str]:
        Specifies the secret containing the connection details that you want to retrieve.
        You can specify either the Amazon Resource Name (ARN) or the friendly name of the secret.
    catalog_id : str, optional
        The ID of the Data Catalog.
        If none is provided, the AWS account ID is used by default.
    dbname: Optional[str]
        Optional database name to overwrite the stored one.
    odbc_driver_version : int
        Major version of the OBDC Driver version that is installed and should be used.
    boto3_session : boto3.Session(), optional
        Boto3 Session. The default boto3 session will be used if boto3_session receive None.
    timeout: Optional[int]
        This is the time in seconds before the connection to the server will time out.
        The default is None which means no timeout.
        This parameter is forwarded to pyodbc.
        https://github.com/mkleehammer/pyodbc/wiki/The-pyodbc-Module#connect

    Returns
    -------
    cx_Oracle.Connection
        cx_Oracle connection.

    Examples
    --------
    >>> import awswrangler as wr
    >>> con = wr.oracle.connect(connection="MY_GLUE_CONNECTION", odbc_driver_version=17)
    >>> with con.cursor() as cursor:
    >>>     cursor.execute("SELECT 1")
    >>>     print(cursor.fetchall())
    >>> con.close()

    """
    attrs: _db_utils.ConnectionAttributes = _db_utils.get_connection_attributes(
        connection=connection, secret_id=secret_id, catalog_id=catalog_id, dbname=dbname, boto3_session=boto3_session
    )
    if attrs.kind != "oracle":
        raise exceptions.InvalidDatabaseType(
            f"Invalid connection type ({attrs.kind}. It must be an oracle connection.)"
        )

    connection_dsn = cx_Oracle.makedsn(attrs.host, attrs.port, service_name=attrs.database)
    connection = cx_Oracle.connect(
        user=attrs.user,
        password=attrs.password,
        dsn=connection_dsn,
    )
    # cx_Oracle.connect does not have a timeout attribute
    connection.call_timeout = timeout
    return connection


@_check_for_cx_Oracle
def read_sql_query(
    sql: str,
    con: "cx_Oracle.Connection",
    index_col: Optional[Union[str, List[str]]] = None,
    params: Optional[Union[List[Any], Tuple[Any, ...], Dict[Any, Any]]] = None,
    chunksize: Optional[int] = None,
    dtype: Optional[Dict[str, pa.DataType]] = None,
    safe: bool = True,
    timestamp_as_object: bool = False,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Return a DataFrame corresponding to the result set of the query string.

    Parameters
    ----------
    sql : str
        SQL query.
    con : cx_Oracle.Connection
        Use cx_Oracle.connect() to use credentials directly or wr.oracle.connect() to fetch it from the Glue Catalog.
    index_col : Union[str, List[str]], optional
        Column(s) to set as index(MultiIndex).
    params :  Union[List, Tuple, Dict], optional
        List of parameters to pass to execute method.
        The syntax used to pass parameters is database driver dependent.
        Check your database driver documentation for which of the five syntax styles,
        described in PEP 249’s paramstyle, is supported.
    chunksize : int, optional
        If specified, return an iterator where chunksize is the number of rows to include in each chunk.
    dtype : Dict[str, pyarrow.DataType], optional
        Specifying the datatype for columns.
        The keys should be the column names and the values should be the PyArrow types.
    safe : bool
        Check for overflows or other unsafe data type conversions.
    timestamp_as_object : bool
        Cast non-nanosecond timestamps (np.datetime64) to objects.

    Returns
    -------
    Union[pandas.DataFrame, Iterator[pandas.DataFrame]]
        Result as Pandas DataFrame(s).

    Examples
    --------
    Reading from Oracle Database using a Glue Catalog Connections

    >>> import awswrangler as wr
    >>> con = wr.oracle.connect(connection="MY_GLUE_CONNECTION", odbc_driver_version=17)
    >>> df = wr.oracle.read_sql_query(
    ...     sql="SELECT * FROM dbo.my_table",
    ...     con=con
    ... )
    >>> con.close()
    """
    _validate_connection(con=con)
    return _db_utils.read_sql_query(
        sql=sql,
        con=con,
        index_col=index_col,
        params=params,
        chunksize=chunksize,
        dtype=dtype,
        safe=safe,
        timestamp_as_object=timestamp_as_object,
    )


@_check_for_cx_Oracle
def read_sql_table(
    table: str,
    con: "cx_Oracle.Connection",
    schema: Optional[str] = None,
    index_col: Optional[Union[str, List[str]]] = None,
    params: Optional[Union[List[Any], Tuple[Any, ...], Dict[Any, Any]]] = None,
    chunksize: Optional[int] = None,
    dtype: Optional[Dict[str, pa.DataType]] = None,
    safe: bool = True,
    timestamp_as_object: bool = False,
) -> Union[pd.DataFrame, Iterator[pd.DataFrame]]:
    """Return a DataFrame corresponding the table.

    Parameters
    ----------
    table : str
        Table name.
    con : cx_Oracle.Connection
        Use cx_Oracle.connect() to use credentials directly or wr.oracle.connect() to fetch it from the Glue Catalog.
    schema : str, optional
        Name of SQL schema in database to query (if database flavor supports this).
        Uses default schema if None (default).
    index_col : Union[str, List[str]], optional
        Column(s) to set as index(MultiIndex).
    params :  Union[List, Tuple, Dict], optional
        List of parameters to pass to execute method.
        The syntax used to pass parameters is database driver dependent.
        Check your database driver documentation for which of the five syntax styles,
        described in PEP 249’s paramstyle, is supported.
    chunksize : int, optional
        If specified, return an iterator where chunksize is the number of rows to include in each chunk.
    dtype : Dict[str, pyarrow.DataType], optional
        Specifying the datatype for columns.
        The keys should be the column names and the values should be the PyArrow types.
    safe : bool
        Check for overflows or other unsafe data type conversions.
    timestamp_as_object : bool
        Cast non-nanosecond timestamps (np.datetime64) to objects.

    Returns
    -------
    Union[pandas.DataFrame, Iterator[pandas.DataFrame]]
        Result as Pandas DataFrame(s).

    Examples
    --------
    Reading from Oracle Database using a Glue Catalog Connections

    >>> import awswrangler as wr
    >>> con = wr.oracle.connect(connection="MY_GLUE_CONNECTION", odbc_driver_version=17)
    >>> df = wr.oracle.read_sql_table(
    ...     table="my_table",
    ...     schema="dbo",
    ...     con=con
    ... )
    >>> con.close()
    """
    table_identifier = _get_table_identifier(schema, table)
    sql: str = f"SELECT * FROM {table_identifier}"
    return read_sql_query(
        sql=sql,
        con=con,
        index_col=index_col,
        params=params,
        chunksize=chunksize,
        dtype=dtype,
        safe=safe,
        timestamp_as_object=timestamp_as_object,
    )


@_check_for_cx_Oracle
@apply_configs
def to_sql(
    df: pd.DataFrame,
    con: "cx_Oracle.Connection",
    table: str,
    schema: str,
    mode: str = "append",
    index: bool = False,
    dtype: Optional[Dict[str, str]] = None,
    varchar_lengths: Optional[Dict[str, int]] = None,
    use_column_names: bool = False,
    chunksize: int = 200,
) -> None:
    """Write records stored in a DataFrame into Microsoft SQL Server.

    Parameters
    ----------
    df : pandas.DataFrame
        Pandas DataFrame https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.html
    con : cx_Oracle.Connection
        Use cx_Oracle.connect() to use credentials directly or wr.oracle.connect() to fetch it from the Glue Catalog.
    table : str
        Table name
    schema : str
        Schema name
    mode : str
        Append or overwrite.
    index : bool
        True to store the DataFrame index as a column in the table,
        otherwise False to ignore it.
    dtype: Dict[str, str], optional
        Dictionary of columns names and Oracle types to be casted.
        Useful when you have columns with undetermined or mixed data types.
        (e.g. {'col name': 'TEXT', 'col2 name': 'FLOAT'})
    varchar_lengths : Dict[str, int], optional
        Dict of VARCHAR length by columns. (e.g. {"col1": 10, "col5": 200}).
    use_column_names: bool
        If set to True, will use the column names of the DataFrame for generating the INSERT SQL Query.
        E.g. If the DataFrame has two columns `col1` and `col3` and `use_column_names` is True, data will only be
        inserted into the database columns `col1` and `col3`.
    chunksize: int
        Number of rows which are inserted with each SQL query. Defaults to inserting 200 rows per query.

    Returns
    -------
    None
        None.

    Examples
    --------
    Writing to Oracle Database using a Glue Catalog Connections

    >>> import awswrangler as wr
    >>> con = wr.oracle.connect(connection="MY_GLUE_CONNECTION", odbc_driver_version=17)
    >>> wr.oracle.to_sql(
    ...     df=df,
    ...     table="table",
    ...     schema="dbo",
    ...     con=con
    ... )
    >>> con.close()

    """
    if df.empty is True:
        raise exceptions.EmptyDataFrame("DataFrame cannot be empty.")
    _validate_connection(con=con)
    try:
        with con.cursor() as cursor:
            _create_table(
                df=df,
                cursor=cursor,
                table=table,
                schema=schema,
                mode=mode,
                index=index,
                dtype=dtype,
                varchar_lengths=varchar_lengths,
            )
            if index:
                df.reset_index(level=df.index.names, inplace=True)
            column_placeholders: str = ", ".join(["?"] * len(df.columns))
            table_identifier = _get_table_identifier(schema, table)
            insertion_columns = ""
            if use_column_names:
                insertion_columns = f"({', '.join(df.columns)})"
            placeholder_parameter_pair_generator = _db_utils.generate_placeholder_parameter_pairs(
                df=df, column_placeholders=column_placeholders, chunksize=chunksize
            )
            for placeholders, parameters in placeholder_parameter_pair_generator:
                sql: str = f"INSERT INTO {table_identifier} {insertion_columns} VALUES {placeholders}"
                _logger.debug("sql: %s", sql)
                cursor.executemany(sql, (parameters,))
            con.commit()
    except Exception as ex:
        con.rollback()
        _logger.error(ex)
        raise
