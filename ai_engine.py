"""
AI and Predictive Maintenance Engine for Smart Printer Monitoring System.
Applies Scikit-Learn machine learning and heuristic models to:
1. Estimate Toner Depletion (remaining days and pages).
2. Predict Component Failure Risk (Roller, Drum, Fuser overheating, Jam probability).
3. Generate Smart Actionable Maintenance Recommendations.
"""

import numpy as np
from sklearn.ensemble import RandomForestClassifier

# Human-readable labels for metrics a real printer may not report; used to
# build the "excluded from this evaluation" note in the output.
METRIC_LABELS = (
    ('toner_black', 'black toner level'),
    ('toner_cyan', 'cyan toner level'),
    ('toner_magenta', 'magenta toner level'),
    ('toner_yellow', 'yellow toner level'),
    ('paper_level', 'paper level'),
    ('drum_wear', 'drum wear'),
    ('fuser_temp', 'fuser temperature'),
    ('total_pages', 'total page count'),
)


class AIPredictiveEngine:
    def __init__(self):
        self.classifier = None
        self._train_baseline_model()

    def _train_baseline_model(self):
        """
        Trains a Random Forest classifier on synthetic printer health telemetry.
        Features: [toner_black, paper_level, drum_wear, fuser_temp, total_pages_scale]
        Labels: 0 -> Healthy, 1 -> Moderate Risk, 2 -> Critical
        """
        np.random.seed(42)
        n_samples = 1200

        toner = np.random.uniform(0, 100, n_samples)
        paper = np.random.uniform(0, 100, n_samples)
        drum = np.random.uniform(0, 100, n_samples)
        fuser = np.random.uniform(160, 240, n_samples)
        pages_k = np.random.uniform(5, 200, n_samples)

        X = np.column_stack([toner, paper, drum, fuser, pages_k])
        y = np.zeros(n_samples, dtype=int)

        for i in range(n_samples):
            if fuser[i] > 220 or drum[i] > 85 or (toner[i] < 5 and paper[i] < 5):
                y[i] = 2  # Critical
            elif fuser[i] > 205 or drum[i] > 65 or toner[i] < 15 or paper[i] < 10:
                y[i] = 1  # Moderate
            else:
                y[i] = 0  # Healthy
        clf = RandomForestClassifier(n_estimators=50, max_depth=6, random_state=42)
        clf.fit(X, y)
        self.classifier = clf


    def evaluate_printer_health(self, telemetry):
        """
        Evaluates real-time printer metrics.
        Returns health_score, failure_probability, risk_category, toner forecasts, component wear, and recommendations.
        """
        if telemetry.get('mode') == 'snmp':
            return self._evaluate_real_printer(telemetry)
        return self._evaluate_simulated(telemetry)

    def _evaluate_real_printer(self, telemetry):
        """
        Health evaluation for real (SNMP/IPP) printers.

        Only values the printer actually reported feed the score; anything
        missing is excluded and listed in `excluded_metrics`. The Random
        Forest needs the complete 5-feature vector, so it only runs when
        every feature is present - real printers never report fuser
        temperature or drum wear, so `failure_probability` is normally None.
        """
        findings = telemetry.get('findings') or []
        report = telemetry.get('scan_report') or {}

        excluded_metrics = [label for key, label in METRIC_LABELS
                            if telemetry.get(key) is None]

        empty = {
            'health_score': None,
            'failure_probability': None,
            'risk_category': 'Unknown',
            'risk_badge': 'secondary',
            'toner_predictions': {'pages_remaining': None, 'days_remaining': None},
            'component_wear': {
                'drum_wear': None,
                'drum_life_remaining': None,
                'fuser_temp': None,
                'fuser_status': 'Not reported',
                'roller_health': None,
            },
            'recommendations': [],
            'excluded_metrics': excluded_metrics,
            'model_excluded': True,
        }

        if not report:
            # First scan has not landed yet: nothing is known about this printer.
            empty['recommendations'] = [{
                'priority': 'Info',
                'badge': 'secondary',
                'action': 'Waiting for the first scan of this printer - its health '
                          'cannot be evaluated yet.',
            }]
            return empty

        if report.get('error') or telemetry.get('status') == 'Offline':
            empty['recommendations'] = [{
                'priority': 'Info',
                'badge': 'secondary',
                'action': 'The printer is not answering SNMP or IPP queries, so its '
                          'health cannot be evaluated right now.',
            }]
            return empty

        # Health score from reported findings and reported levels only.
        health = 100
        for finding in findings:
            severity = finding.get('severity')
            if severity == 'critical':
                health -= 30
            elif severity == 'warning':
                health -= 12
            else:
                health -= 4

        toner = telemetry.get('toner_black')
        if toner is not None:
            if toner <= 10:
                health -= 15
            elif toner <= 25:
                health -= 8
        paper = telemetry.get('paper_level')
        if paper is not None and paper <= 15:
            health -= 10

        health_score = max(0, min(100, health))

        if health_score >= 75:
            risk_category, risk_badge = 'Healthy', 'success'
        elif health_score >= 45:
            risk_category, risk_badge = 'Moderate Risk', 'warning'
        else:
            risk_category, risk_badge = 'Critical Attention', 'danger'

        # Random Forest: runs only with the complete feature vector.
        feature_keys = ('toner_black', 'paper_level', 'drum_wear', 'fuser_temp', 'total_pages')
        model_excluded = not all(telemetry.get(key) is not None for key in feature_keys)
        failure_probability = None
        if not model_excluded:
            feature_vec = np.array([
                [float(telemetry[key]) for key in feature_keys[:4]]
                + [float(telemetry['total_pages']) / 1000.0]
            ])
            class_probs = self.classifier.predict_proba(feature_vec)[0]
            prob_moderate = class_probs[1] if len(class_probs) > 1 else 0.0
            prob_critical = class_probs[2] if len(class_probs) > 2 else 0.0
            failure_probability = round((prob_moderate * 0.45 + prob_critical * 1.0) * 100, 1)
            failure_probability = max(2.0, min(98.5, failure_probability))

        # Toner depletion forecast from the reported level only.
        pages_remaining = days_remaining = None
        if toner is not None:
            pages_remaining = int((toner / 100.0) * 2500)
            days_remaining = max(1, int(pages_remaining / 85))

        fuser_status = 'Not reported'
        if any(f.get('code') == 'overheat' for f in findings):
            fuser_status = 'Printer reports overheating'

        recommendations = []
        for finding in findings:
            message = ' '.join(part for part in (
                finding.get('plain_message'), finding.get('what_to_do')) if part)
            if not message:
                message = finding.get('title') or 'Printer reported a problem.'
            severity = finding.get('severity')
            recommendations.append({
                'priority': 'High' if severity == 'critical'
                else 'Medium' if severity == 'warning' else 'Info',
                'badge': 'danger' if severity == 'critical'
                else 'warning' if severity == 'warning' else 'info',
                'action': message,
            })
        if not recommendations:
            recommendations.append({
                'priority': 'Info',
                'badge': 'success',
                'action': 'No problems reported by this printer.',
            })

        return {
            'health_score': health_score,
            'failure_probability': failure_probability,
            'risk_category': risk_category,
            'risk_badge': risk_badge,
            'toner_predictions': {'pages_remaining': pages_remaining,
                                  'days_remaining': days_remaining},
            'component_wear': {
                'drum_wear': None,
                'drum_life_remaining': None,
                'fuser_temp': None,
                'fuser_status': fuser_status,
                'roller_health': None,
            },
            'recommendations': recommendations,
            'excluded_metrics': excluded_metrics,
            'model_excluded': model_excluded,
        }

    def _evaluate_simulated(self, telemetry):
        toner = float(telemetry.get('toner_black', 100))
        paper = float(telemetry.get('paper_level', 100))
        drum = float(telemetry.get('drum_wear', 10))
        fuser_temp = float(telemetry.get('fuser_temp', 185))
        total_pages = float(telemetry.get('total_pages', 10000))
        jammed = bool(telemetry.get('jammed', False))

        pages_k = total_pages / 1000.0
        feature_vec = np.array([[toner, paper, drum, fuser_temp, pages_k]])
        
        class_probs = self.classifier.predict_proba(feature_vec)[0]
        prob_moderate = class_probs[1] if len(class_probs) > 1 else 0.0
        prob_critical = class_probs[2] if len(class_probs) > 2 else 0.0

        failure_probability = round((prob_moderate * 0.45 + prob_critical * 1.0) * 100, 1)
        failure_probability = max(2.0, min(98.5, failure_probability))

        # Overall Health Score (0 - 100)
        health_deductions = 0
        if jammed:
            health_deductions += 60
        if toner < 20:
            health_deductions += (20 - toner) * 1.5
        if paper < 15:
            health_deductions += (15 - paper) * 1.0
        if drum > 50:
            health_deductions += (drum - 50) * 1.2
        if fuser_temp > 200:
            health_deductions += (fuser_temp - 200) * 1.5
        elif fuser_temp < 165:
            health_deductions += 15

        health_score = max(5, min(100, int(100 - health_deductions)))

        if health_score >= 75 and failure_probability < 30:
            risk_category = "Healthy"
            risk_badge = "success"
        elif health_score >= 45 and failure_probability < 65:
            risk_category = "Moderate Risk"
            risk_badge = "warning"
        else:
            risk_category = "Critical Attention"
            risk_badge = "danger"

        # Toner Depletion & Remaining Calculations
        avg_pages_per_day = 85
        pages_remaining = int((toner / 100.0) * 2500)
        days_remaining = max(1, int(pages_remaining / avg_pages_per_day))

        # Component Wear Metrics
        drum_life_remaining = max(0, int(100 - drum))
        fuser_status = "Optimal"
        if fuser_temp > 215:
            fuser_status = "Overheating Warning"
        elif fuser_temp > 200:
            fuser_status = "Elevated Temp"
        elif fuser_temp < 165:
            fuser_status = "Sub-optimal Temp"

        roller_health = max(5, int(100 - (total_pages % 20000) / 200.0))

        # Recommendations
        recommendations = []
        if jammed:
            recommendations.append({
                'priority': 'High',
                'badge': 'danger',
                'action': 'Paper jam latched in the duplex feeder path. Open the rear cover and clear the sheet.'
            })
        if toner <= 10:
            recommendations.append({
                'priority': 'High',
                'badge': 'danger',
                'action': 'Replace Toner Cartridge immediately (under 10% remaining).'
            })
        elif toner <= 25:
            recommendations.append({
                'priority': 'Medium',
                'badge': 'warning',
                'action': f'Order replacement black toner cartridge. Estimated {days_remaining} days left ({pages_remaining} pages).'
            })

        if paper <= 15:
            recommendations.append({
                'priority': 'High',
                'badge': 'danger',
                'action': 'Paper Tray critically low. Refill Tray 1 and Tray 2 to prevent print aborts.'
            })

        if drum >= 80:
            recommendations.append({
                'priority': 'High',
                'badge': 'danger',
                'action': f'Drum unit wear at {int(drum)}%. Schedule immediate technician drum replacement to avoid banding.'
            })
        elif drum >= 60:
            recommendations.append({
                'priority': 'Medium',
                'badge': 'warning',
                'action': f'Imaging drum wear is elevated ({int(drum)}%). Inspect unit during next maintenance cycle.'
            })

        if fuser_temp > 210:
            recommendations.append({
                'priority': 'High',
                'badge': 'danger',
                'action': f'Fuser core temperature high ({fuser_temp}°C). Check cooling airflows or thermistor.'
            })

        if roller_health < 30:
            recommendations.append({
                'priority': 'Medium',
                'badge': 'warning',
                'action': 'Pickup roller friction degraded. Clean with isopropyl alcohol or replace feed rollers.'
            })

        if not recommendations:
            recommendations.append({
                'priority': 'Info',
                'badge': 'success',
                'action': 'All subsystems within operational tolerances. No maintenance actions required.'
            })

        return {
            'health_score': health_score,
            'failure_probability': failure_probability,
            'risk_category': risk_category,
            'risk_badge': risk_badge,
            'toner_predictions': {
                'pages_remaining': pages_remaining,
                'days_remaining': days_remaining
            },
            'component_wear': {
                'drum_wear': int(drum),
                'drum_life_remaining': drum_life_remaining,
                'fuser_temp': int(fuser_temp),
                'fuser_status': fuser_status,
                'roller_health': roller_health
            },
            'recommendations': recommendations
        }


# Global singleton engine instance
ai_engine = AIPredictiveEngine()



