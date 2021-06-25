# Copyright 2019-2021 AstroLab Software
# Authors: Julien Peloton, Juliette Vlieghe
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pyspark.sql.functions import pandas_udf, PandasUDFType
from pyspark.sql.types import BooleanType

import numpy as np
import pandas as pd
import datetime
import requests
import os
import logging

from astropy.coordinates import SkyCoord
from astropy.coordinates import Angle
from astropy import units as u
from astropy.time import Time

from fink_science.conversion import dc_mag


@pandas_udf(BooleanType(), PandasUDFType.SCALAR)
def rate_based_kn_candidates(
        objectId, rfscore, snn_snia_vs_nonia, snn_sn_vs_all, drb,
        classtar, jdstarthist, ndethist, cdsxmatch, ra, dec, ssdistnr, cjdc,
        cfidc, cmagpsfc, csigmapsfc, cmagnrc, csigmagnrc, cmagzpscic,
        cisdiffposc
        ) -> pd.Series:
    """
    Return alerts considered as KN candidates.

    The cuts are based on Andreoni et al. 2021 https://arxiv.org/abs/2104.06352

    If the environment variable KNWEBHOOK is defined and match a webhook url,
    the alerts that pass the filter will be sent to the matching Slack channel.

    Parameters
    ----------
    objectId: Spark DataFrame Column
        Column containing the alert IDs
    rfscore, snn_snia_vs_nonia, snn_sn_vs_all: Spark DataFrame Columns
        Columns containing the scores for: 'Early SN Ia',
        'Ia SN vs non-Ia SN', 'SN Ia and Core-Collapse vs non-SN events'
    drb: Spark DataFrame Column
        Column containing the Deep-Learning Real Bogus score
    classtar: Spark DataFrame Column
        Column containing the sextractor score
    jdstarthist: Spark DataFrame Column
        Column containing earliest Julian dates of epoch [days]
    ndethist: Spark DataFrame Column
        Column containing the number of prior detections (theshold of 3 sigma)
    cdsxmatch: Spark DataFrame Column
        Column containing the cross-match values
    ra: Spark DataFrame Column
        Column containing the right Ascension of candidate; J2000 [deg]
    dec: Spark DataFrame Column
        Column containing the declination of candidate; J2000 [deg]
    ssdistnr: Spark DataFrame Column
        distance to nearest known solar system object; -999.0 if none [arcsec]
    cjdc, cfidc, cmagpsfc, csigmapsfc, cmagnrc, csigmagnrc, cmagzpscic: Spark DataFrame Columns
        Columns containing history of fid, magpsf, sigmapsf, magnr, sigmagnr,
        magzpsci, isdiffpos as arrays
    Returns
    ----------
    out: pandas.Series of bool
        Return a Pandas DataFrame with the appropriate flag:
        false for bad alert, and true for good alert.
    """
    # Extract last (new) measurement from the concatenated column
    jd = cjdc.apply(lambda x: x[-1])
    fid = cfidc.apply(lambda x: x[-1])
    isdiffpos = cisdiffposc.apply(lambda x: x[-1])

    high_drb = drb.astype(float) > 0.9
    high_classtar = classtar.astype(float) > 0.4
    new_detection = jd.astype(float) - jdstarthist.astype(float) < 14
    small_detection_history = ndethist.astype(float) < 20
    appeared = isdiffpos.astype(str) == 't'
    far_from_mpc = (ssdistnr.astype(float) > 10) | (ssdistnr.astype(float) < 0)

    list_simbad_galaxies = [
        "galaxy",
        "Galaxy",
        "EmG",
        "Seyfert",
        "Seyfert_1",
        "Seyfert_2",
        "BlueCompG",
        "StarburstG",
        "LSB_G",
        "HII_G",
        "High_z_G",
        "GinPair",
        "GinGroup",
        "BClG",
        "GinCl",
        "PartofG",
    ]

    keep_cds = \
        ["Unknown", "Transient", "Fail"] + list_simbad_galaxies

    f_kn = high_drb & high_classtar & new_detection & small_detection_history
    f_kn = f_kn & cdsxmatch.isin(keep_cds) & appeared & far_from_mpc

    if f_kn.any():
        # Galactic latitude transformation
        b = SkyCoord(
            np.array(ra[f_kn], dtype=float),
            np.array(dec[f_kn], dtype=float),
            unit='deg'
        ).galactic.b.deg

        # Simplify notations
        ra = Angle(
            np.array(ra.astype(float)[f_kn]) * u.degree
        ).deg
        dec = Angle(
            np.array(dec.astype(float)[f_kn]) * u.degree
        ).deg
        ra_formatted = Angle(ra * u.degree).to_string(
            precision=2, sep=' ', unit=u.hour
            )
        dec_formatted = Angle(dec * u.degree).to_string(
            precision=1, sep=' ', alwayssign=True
            )
        delta_jd_first = np.array(
            jd.astype(float)[f_kn] - jdstarthist.astype(float)[f_kn]
        )
        rfscore = np.array(rfscore.astype(float)[f_kn])
        snn_snia_vs_nonia = np.array(snn_snia_vs_nonia.astype(float)[f_kn])
        snn_sn_vs_all = np.array(snn_sn_vs_all.astype(float)[f_kn])

        # Redefine jd & fid relative to candidates
        fid = np.array(fid.astype(int)[f_kn])
        jd = np.array(jd)[f_kn]

    dict_filt = {1: 'g', 2: 'r'}
    rate_all = []
    for i, alertID in enumerate(objectId[f_kn]):
        # Careful - Spark casts None as NaN!
        maskNotNone = ~np.isnan(np.array(cmagpsfc[f_kn].values[i]))

        # Time since last detection (independently of the band)
        jd_hist_allbands = np.array(np.array(cjdc[f_kn])[i])[maskNotNone]
        if len(jd_hist_allbands) < 2:
            rate_all.append(0)
            continue
        delta_jd_last = jd_hist_allbands[-1] - jd_hist_allbands[-2]

        filt = fid[i]
        maskFilter = np.array(cfidc[f_kn].values[i]) == filt
        m = maskNotNone * maskFilter
        if sum(m) < 2:
            rate_all.append(0)
            continue
        # DC mag (history + last measurement)
        mag_hist, err_hist = np.array([
            dc_mag(k[0], k[1], k[2], k[3], k[4], k[5], k[6])
            for k in zip(
                cfidc[f_kn].values[i][m][-2:],
                cmagpsfc[f_kn].values[i][m][-2:],
                csigmapsfc[f_kn].values[i][m][-2:],
                cmagnrc[f_kn].values[i][m][-2:],
                csigmagnrc[f_kn].values[i][m][-2:],
                cmagzpscic[f_kn].values[i][m][-2:],
                cisdiffposc[f_kn].values[i][m][-2:],
            )
        ]).T

        # Grab the last measurement and its error estimate
        mag = mag_hist[-1]
        err_mag = err_hist[-1]

        # Compute rate only if more than 1 measurement available
        if len(mag_hist) > 1:
            jd_hist = cjdc[f_kn].values[i][m]

            # rate is between `last` and `last-1` measurements only
            dmag = mag_hist[-1] - mag_hist[-2]
            dt = jd_hist[-1] - jd_hist[-2]
            rate = dmag / dt
            error_rate = np.sqrt(err_hist[-1]**2 + err_hist[-2]**2) / dt

        # filter messages on rate
        rate_all.append(rate)
        if rate <= 0.3:
            continue

        # information to send
        alert_text = """
            *New kilonova candidate:* <http://134.158.75.151:24000/{}|{}>
            """.format(alertID, alertID)
        score_text = """
            *Scores:*\n- Early SN Ia: {:.2f}\n- Ia SN vs non-Ia SN: {:.2f}\n- SN Ia and Core-Collapse vs non-SN: {:.2f}
            """.format(rfscore[i], snn_snia_vs_nonia[i], snn_sn_vs_all[i])
        time_text = """
            *Time:*\n- {} UTC\n - Time since last detection: {:.1f} days\n - Time since first detection: {:.1f} days
            """.format(Time(jd[i], format='jd').iso, delta_jd_last, delta_jd_first[i])
        measurements_text = """
            *Measurement (band {}):*\n- Apparent magnitude: {:.2f} ± {:.2f} \n- Rate: ({:.2f} ± {:.2f}) mag/day\n
            """.format(dict_filt[fid[i]], mag, err_mag, rate, error_rate)
        radec_text = """
             *RA/Dec:*\n- [hours, deg]: {} {}\n- [deg, deg]: {:.7f} {:+.7f}
             """.format(ra_formatted[i], dec_formatted[i], ra[i], dec[i])
        galactic_position_text = """
            *Galactic latitude:*\n- [deg]: {:.7f}""".format(b[i])

        tns_text = '*TNS:* <https://www.wis-tns.org/search?ra={}&decl={}&radius=5&coords_unit=arcsec|link>'.format(ra[i], dec[i])
        # message formatting
        blocks = [
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": alert_text
                    },
                ]
             },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": time_text
                    },
                    {
                        "type": "mrkdwn",
                        "text": score_text
                    },
                    {
                        "type": "mrkdwn",
                        "text": radec_text
                    },
                    {
                        "type": "mrkdwn",
                        "text": measurements_text
                    },
                    {
                        "type": "mrkdwn",
                        "text": galactic_position_text
                    },
                    {
                        "type": "mrkdwn",
                        "text": tns_text
                    },
                ]
            },
        ]

        error_message = """
        {} is not defined as env variable
        if an alert has passed the filter,
        the message has not been sent to Slack
        """
        for url_name in ['KNWEBHOOK', 'KNWEBHOOK_FINK']:
            if (url_name in os.environ):
                requests.post(
                    os.environ[url_name],
                    json={
                        'blocks': blocks,
                        'username': 'Rate-based kilonova bot'
                    },
                    headers={'Content-Type': 'application/json'},
                )
            else:
                log = logging.Logger('Kilonova filter')
                log.warning(error_message.format(url_name))

        ama_in_env = ('KNWEBHOOK_AMA_RATE' in os.environ)

        # Send alerts to amateurs only on Friday
        now = datetime.datetime.utcnow()

        # Monday is 1 and Sunday is 7
        is_friday = (now.isoweekday() == 5)

        if (np.abs(b[i]) > 20) & (mag < 20) & is_friday & ama_in_env:
            requests.post(
                os.environ['KNWEBHOOK_AMA_RATE'],
                json={
                    'blocks': blocks,
                    'username': 'Rate-based kilonova bot'
                },
                headers={'Content-Type': 'application/json'},
            )
        else:
            log = logging.Logger('Kilonova filter')
            log.warning(error_message.format(url_name))

    f_kn.loc[f_kn] = np.array(rate_all) > 0.3

    return f_kn
